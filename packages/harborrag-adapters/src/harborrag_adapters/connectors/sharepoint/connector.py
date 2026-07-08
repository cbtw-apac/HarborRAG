from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from typing import Any, Protocol

import requests
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    DocumentProcessingError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.http_utils import (
    require_same_origin_url,
    retry_delay_seconds,
)
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery

from .config import SharePointSiteConfig
from .mappers import (
    build_document_metadata,
    build_source_record,
    drive_item_id_from_record,
    parse_timestamp,
)
from .utils import (
    DRIVE_ITEM_SELECT,
    DRIVE_SELECT,
    children_endpoint,
    is_drive_file,
    is_drive_folder,
    item_extension,
    item_hidden,
    item_mime_type,
    item_name,
    matches_pattern,
    site_path_endpoint,
)


logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SharePointClient(Protocol):
    """Small Microsoft Graph API surface needed by ``SharePointConnector``."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def get_bytes(self, endpoint: str) -> bytes:
        ...


class SharePointConnector(BaseConnector):
    """Connector for SharePoint document libraries through Microsoft Graph.

    Discovery resolves a site and drive, then walks drive items from the
    configured root or explicit item IDs. Loading downloads file content through
    Graph and preserves drive metadata for downstream provenance.
    """

    provider_name = "sharepoint"
    capabilities = ConnectorCapabilities(
        pagination=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
    )

    def __init__(
        self,
        config: SharePointSiteConfig,
        *,
        client: SharePointClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or _RequestsGraphClient(config)
        self._site: dict[str, Any] | None = None
        self._drive: dict[str, Any] | None = None

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Discover SharePoint drive-item records from IDs or folder traversal."""
        query = query or ConnectorQuery()
        site = self._resolve_site()
        drive = self._resolve_drive(site)
        logger.info(
            "Discovering SharePoint files in site=%s drive=%s",
            site.get("id"),
            drive.get("id"),
        )

        yielded = 0
        item_ids = self._item_ids_from_query(query)
        if item_ids:
            for item_id in item_ids:
                for record in self._records_from_item_id(
                    item_id,
                    site=site,
                    drive=drive,
                    query=query,
                ):
                    yield record
                    yielded += 1
                    if query.limit is not None and yielded >= query.limit:
                        return
            return

        root_path = query.path or self.config.root_path
        root_item_id = self._root_item_id_from_query(query)
        for record in self._walk_children(
            site=site,
            drive=drive,
            query=query,
            item_id=root_item_id,
            path=None if root_item_id else root_path,
        ):
            yield record
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Download one SharePoint drive file as a raw document."""
        site = self._resolve_site()
        drive = self._resolve_drive(site)
        drive_id = str(record.metadata.get("drive_id") or drive.get("id"))
        item_id = drive_item_id_from_record(record)
        item = self._get_item(drive_id, item_id)

        if not is_drive_file(item):
            raise DocumentProcessingError(
                f"SharePoint drive item {item_id} is not a downloadable file"
            )
        self._enforce_size_limit(item)

        logger.info(
            "Loading SharePoint file item=%s drive=%s name=%s",
            item_id,
            drive_id,
            item_name(item),
        )
        content = self.client.get_bytes(f"drives/{drive_id}/items/{item_id}/content")
        checksum = hashlib.sha256(content).hexdigest()
        metadata = build_document_metadata(
            item,
            site=site,
            drive=drive,
            checksum=checksum,
        )

        return RawDocument(
            id=record.id,
            source=str(item.get("webUrl") or record.metadata.get("web_url") or item_id),
            content=content,
            content_type=item_mime_type(item),
            metadata=metadata,
            raw=item,
        )

    def load_by_ids(self, item_ids: list[str]) -> Iterator[RawDocument]:
        """Convenience loader for callers that already have drive item IDs."""
        for item_id in item_ids:
            yield self.load(self._record_for_item_id(item_id))

    def _records_from_item_id(
        self,
        item_id: str,
        *,
        site: dict[str, Any],
        drive: dict[str, Any],
        query: ConnectorQuery,
    ) -> Iterator[SourceRecord]:
        """Return records for a direct drive item ID, descending into folders."""
        item = self._get_item(str(drive["id"]), item_id)
        if is_drive_file(item):
            if self._should_process_file(item, query):
                yield build_source_record(
                    item,
                    site_id=str(site["id"]),
                    drive_id=str(drive["id"]),
                )
            return

        if is_drive_folder(item):
            yield from self._walk_children(
                site=site,
                drive=drive,
                query=query,
                item_id=item_id,
            )

    def _walk_children(
        self,
        *,
        site: dict[str, Any],
        drive: dict[str, Any],
        query: ConnectorQuery,
        item_id: str | None = None,
        path: str | None = None,
    ) -> Iterator[SourceRecord]:
        """Walk child items breadth-first enough to yield files before recursion."""
        drive_id = str(drive["id"])
        folder_ids: list[str] = []
        for item in self._iter_children(drive_id, item_id=item_id, path=path):
            if is_drive_file(item):
                if self._should_process_file(item, query):
                    yield build_source_record(
                        item,
                        site_id=str(site["id"]),
                        drive_id=drive_id,
                    )
                continue

            if query.recursive and is_drive_folder(item):
                child_id = str(item.get("id") or "")
                if child_id:
                    folder_ids.append(child_id)

        for child_id in folder_ids:
            yield from self._walk_children(
                site=site,
                drive=drive,
                query=query,
                item_id=child_id,
            )

    def _iter_children(
        self,
        drive_id: str,
        *,
        item_id: str | None = None,
        path: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate Graph children pages for a folder item or path."""
        endpoint = children_endpoint(drive_id, item_id=item_id, path=path)
        params: dict[str, Any] | None = {
            "$top": self.config.page_size,
            "$select": DRIVE_ITEM_SELECT,
        }

        while endpoint:
            response = self.client.get_json(endpoint, params=params)
            yield from response.get("value", [])
            endpoint = response.get("@odata.nextLink")
            params = None

    def _iter_site_drives(self, site_id: str) -> Iterator[dict[str, Any]]:
        """Iterate drives for a site when resolving a configured drive name."""
        endpoint = f"sites/{site_id}/drives"
        params: dict[str, Any] | None = {
            "$top": self.config.page_size,
            "$select": DRIVE_SELECT,
        }

        while endpoint:
            response = self.client.get_json(endpoint, params=params)
            yield from response.get("value", [])
            endpoint = response.get("@odata.nextLink")
            params = None

    def _get_item(self, drive_id: str, item_id: str) -> dict[str, Any]:
        """Fetch one drive item with the fields used by mappers and filters."""
        return self.client.get_json(
            f"drives/{drive_id}/items/{item_id}",
            params={"$select": DRIVE_ITEM_SELECT},
        )

    def _resolve_site(self) -> dict[str, Any]:
        """Resolve and cache the configured SharePoint site."""
        if self._site is not None:
            return self._site

        if self.config.site_id:
            self._site = {
                "id": self.config.site_id,
                "name": self.config.hostname,
                "webUrl": self.config.site_url,
            }
            return self._site

        endpoint = site_path_endpoint(
            str(self.config.hostname),
            self.config.site_path,
        )
        site = self.client.get_json(endpoint)
        if not site.get("id"):
            raise FetchError("SharePoint site response did not include id")
        self._site = site
        logger.debug("Resolved SharePoint site %s", site.get("id"))
        return site

    def _resolve_drive(self, site: dict[str, Any]) -> dict[str, Any]:
        """Resolve and cache the configured or default document library drive."""
        if self._drive is not None:
            return self._drive

        if self.config.drive_id:
            self._drive = {"id": self.config.drive_id, "name": self.config.drive_name}
            return self._drive

        site_id = str(site["id"])
        if self.config.drive_name:
            expected = self.config.drive_name.casefold()
            for drive in self._iter_site_drives(site_id):
                if str(drive.get("name") or "").casefold() == expected:
                    self._drive = drive
                    logger.debug("Resolved SharePoint drive %s", drive.get("id"))
                    return drive
            raise FetchError(
                f"SharePoint drive named {self.config.drive_name!r} was not found"
            )

        drive = self.client.get_json(
            f"sites/{site_id}/drive",
            params={"$select": DRIVE_SELECT},
        )
        if not drive.get("id"):
            raise FetchError("SharePoint default drive response did not include id")
        self._drive = drive
        logger.debug("Resolved SharePoint default drive %s", drive.get("id"))
        return drive

    def _should_process_file(self, item: dict[str, Any], query: ConnectorQuery) -> bool:
        """Apply query/config filters to one Microsoft Graph drive item."""
        name = item_name(item)
        mime_type = item_mime_type(item)
        size = int(item.get("size") or 0)
        extension = item_extension(item)

        if item_hidden(item) and not self.config.include_hidden:
            logger.debug("Skipping hidden SharePoint file %s", name)
            return False
        if self.config.max_file_size_bytes is not None:
            if size > self.config.max_file_size_bytes:
                logger.debug("Skipping oversized SharePoint file %s", name)
                return False
        if self.config.allowed_extensions and extension not in self.config.allowed_extensions:
            logger.debug("Skipping SharePoint file outside allowed extensions %s", name)
            return False
        if extension in self.config.excluded_extensions:
            logger.debug("Skipping SharePoint file with excluded extension %s", name)
            return False
        if query.updated_after:
            updated_at = parse_timestamp(item.get("lastModifiedDateTime"))
            if updated_at and updated_at <= query.updated_after:
                return False
        if not matches_pattern(item, query.pattern):
            return False

        if self.config.process_file_callback:
            try:
                should_process, reason = self.config.process_file_callback(
                    name,
                    size,
                    mime_type,
                )
            except Exception:
                if self.config.fail_on_error:
                    raise
                logger.exception("SharePoint file callback failed for %s", name)
                return False
            if not should_process:
                logger.debug("Skipping SharePoint file %s: %s", name, reason)
                return False
        return True

    def _enforce_size_limit(self, item: dict[str, Any]) -> None:
        """Prevent large drive files from being downloaded by direct loads."""
        size = int(item.get("size") or 0)
        if self.config.max_file_size_bytes is None or not size:
            return
        if size > self.config.max_file_size_bytes:
            raise DocumentProcessingError(
                f"SharePoint file {item_name(item)!r} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}"
            )

    def _record_for_item_id(self, item_id: str) -> SourceRecord:
        site = self._resolve_site()
        drive = self._resolve_drive(site)
        item = self._get_item(str(drive["id"]), item_id)
        return build_source_record(
            item,
            site_id=str(site["id"]),
            drive_id=str(drive["id"]),
        )

    @staticmethod
    def _item_ids_from_query(query: ConnectorQuery) -> list[str]:
        values = (
            query.filters.get("item_ids")
            or query.filters.get("drive_item_ids")
            or query.filters.get("file_ids")
        )
        if values is None:
            return []
        if isinstance(values, str):
            return [values]
        return [str(value) for value in values]

    @staticmethod
    def _root_item_id_from_query(query: ConnectorQuery) -> str | None:
        value = query.filters.get("root_item_id") or query.filters.get("folder_item_id")
        return str(value) if value else None


class _RequestsGraphClient:
    """Authenticated, rate-limited Microsoft Graph client."""

    def __init__(self, config: SharePointSiteConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0
        self._token: str | None = None
        self._token_expires_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a Graph endpoint and decode its JSON body."""
        response = self._request("GET", self._api_url(endpoint), params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"Microsoft Graph returned non-JSON for {endpoint}") from exc

    def get_bytes(self, endpoint: str) -> bytes:
        """GET a Graph endpoint that returns file bytes."""
        response = self._request(
            "GET",
            self._api_url(endpoint),
            headers={"Accept": "*/*"},
        )
        return response.content

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one Graph request with auth, local rate limiting, and retries."""
        last_error: Exception | None = None
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._access_token()}"

        for attempt in range(self.config.max_retries + 1):
            self._acquire()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.config.request_timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    raise FetchError(str(exc)) from exc
                self._sleep(attempt, exc)
                continue

            if response.status_code in (401, 403):
                raise AuthenticationError(response.text)
            if response.status_code == 429 and attempt == self.config.max_retries:
                raise RateLimitError(response.text)
            if (
                response.status_code not in _RETRYABLE_STATUS
                or attempt == self.config.max_retries
            ):
                if response.status_code >= 400:
                    raise FetchError(
                        f"Microsoft Graph request failed with HTTP "
                        f"{response.status_code}: {response.text}"
                    )
                return response

            last_error = FetchError(
                f"Microsoft Graph request returned HTTP {response.status_code}"
            )
            self._sleep(attempt, last_error, response.headers)

        raise FetchError(str(last_error))

    def _access_token(self) -> str:
        """Return a configured token or obtain/cache one by client credentials."""
        if self.config.access_token:
            return self.config.access_token
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token

        token_url = (
            f"https://login.microsoftonline.com/{self.config.tenant_id}"
            "/oauth2/v2.0/token"
        )
        try:
            response = self.session.post(
                token_url,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "client_credentials",
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=self.config.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AuthenticationError(str(exc)) from exc

        if response.status_code >= 400:
            raise AuthenticationError(response.text)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthenticationError("Microsoft identity returned non-JSON token") from exc

        token = payload.get("access_token")
        if not token:
            raise AuthenticationError("Microsoft identity token response missing token")
        self._token = str(token)
        self._token_expires_at = time.monotonic() + int(payload.get("expires_in") or 3599)
        return self._token

    def _api_url(self, endpoint: str) -> str:
        """Build a Graph API URL while rejecting cross-origin absolute URLs."""
        if endpoint.startswith(("http://", "https://")):
            try:
                return require_same_origin_url(
                    endpoint,
                    self.config.graph_api_url,
                    label="Microsoft Graph",
                )
            except ValueError as exc:
                raise FetchError(str(exc)) from exc
        return f"{self.config.graph_api_url}/{endpoint.lstrip('/')}"

    def _acquire(self) -> None:
        """Throttle requests according to the configured per-minute budget."""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep(self, attempt: int, error: Exception, headers: Any = None) -> None:
        """Sleep before retrying, honoring provider retry headers when present."""
        fallback_delay = self.config.backoff_factor * (2**attempt)
        delay = retry_delay_seconds(headers, fallback_delay)
        logger.warning(
            "Retrying Microsoft Graph request after error, attempt %d/%d: %s",
            attempt + 1,
            self.config.max_retries,
            error,
        )
        if delay > 0:
            time.sleep(delay)
