from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any, Protocol

import requests
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.attachments import AttachmentProcessor
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
from harborrag_adapters.connectors.utils import extend_with_limit
from harborrag_adapters.parsers import HarborParser

from .config import ConfluenceDeploymentType, ConfluenceSpaceConfig
from .mappers import (
    body_html_from_content,
    build_document_metadata,
    build_source_record,
    content_id_from_record,
)
from .utils import (
    COMMENT_EXPAND,
    CONTENT_EXPAND,
    LIGHT_EXPAND,
    build_cql,
    build_search_params,
    extract_cursor,
)


logger = logging.getLogger("harborrag.adapters.connectors.confluence")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ConfluenceClient(Protocol):
    """Small API surface needed by ``ConfluenceConnector``.

    Tests can provide this protocol without constructing a real authenticated
    requests session.
    """

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def download_bytes(self, url: str) -> bytes | None:
        ...


class ConfluenceConnector(BaseConnector):
    """Connector for Confluence Cloud and Data Center REST APIs.

    Discovery returns page/blogpost source records from CQL search. Loading a
    record fetches the expanded content body and optional comments/attachments so
    downstream parsing receives one complete page document.
    """

    provider_name = "confluence"
    capabilities = ConnectorCapabilities(
        pagination=True,
        attachments=True,
        comments=True,
        labels=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
    )

    def __init__(
        self,
        config: ConfluenceSpaceConfig,
        *,
        client: ConfluenceClient | None = None,
        parser: HarborParser | None = None,
    ) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.client = client or _RequestsConfluenceClient(config)
        self._attachments = AttachmentProcessor(
            download_fn=self.client.download_bytes,
            base_url=self.base_url,
            parser=parser,
            custom_parsers=config.custom_parsers,
            process_attachment_callback=config.process_attachment_callback,
            max_attachment_size_bytes=config.max_attachment_size_bytes,
            fail_on_error=config.fail_on_error,
            logger_=logger,
        )

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Search Confluence content or materialize explicitly requested IDs."""
        query = query or ConnectorQuery()
        content_ids = self._content_ids_from_query(query)
        if content_ids:
            ids = list(self._with_children(content_ids, query))
            for content_id in ids[: query.limit]:
                yield self._record_for_id(content_id, query)
            return

        cql = self._cql_from_query(query)
        yielded = 0
        for content in self._search(cql):
            if not self._should_process_content(content):
                continue
            record = build_source_record(
                content,
                base_url=self.base_url,
                deployment_type=self.config.deployment_type,
                default_space_key=self.config.space_key,
            )
            record.metadata["include_attachments"] = query.include_attachments
            yield record
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one expanded Confluence content item as an HTML raw document."""
        content_id = content_id_from_record(record)
        content = self._get_content(content_id)

        if not self._should_process_content(content):
            raise DocumentProcessingError(
                f"Confluence content {content_id} does not match label filters"
            )

        self._validate_content(content, content_id)
        comments = self._fetch_comments(content_id) if self.config.include_comments else []
        attachments = []
        include_attachments = bool(
            self.config.include_attachments
            and record.metadata.get("include_attachments", True)
        )
        if include_attachments:
            attachments = self._attachments.process(self._list_attachments(content_id))

        metadata = build_document_metadata(
            content,
            base_url=self.base_url,
            deployment_type=self.config.deployment_type,
            comments=comments,
            attachments=attachments,
        )
        body_html = body_html_from_content(content)

        return RawDocument(
            id=record.id,
            source=metadata["display_url"],
            content=body_html,
            content_type="text/html",
            metadata=metadata,
            raw=content,
        )

    def load_by_ids(self, content_ids: list[str]) -> Iterator[RawDocument]:
        """Convenience loader for callers that already have Confluence IDs."""
        for content_id in content_ids:
            yield self.load(self._record_for_id(content_id, ConnectorQuery()))

    def _search(self, cql: str) -> Iterator[dict[str, Any]]:
        """Iterate CQL results across Cloud cursor and Data Center start paging."""
        cursor: str | None = None
        start = 0
        while True:
            params = build_search_params(
                cql=cql,
                limit=self.config.page_size,
                start=start,
                cursor=cursor,
                expand=LIGHT_EXPAND,
            )
            response = self.client.get_json("content/search", params=params)
            results = response.get("results", [])
            if not results:
                return
            yield from results

            next_url = response.get("_links", {}).get("next")
            next_cursor = extract_cursor(next_url)
            if next_cursor:
                cursor = next_cursor
                continue
            if not next_url and len(results) < self.config.page_size:
                return
            cursor = None
            start += len(results)

    def _get_content(self, content_id: str) -> dict[str, Any]:
        """Fetch the fully expanded page/blogpost needed for loading."""
        return self.client.get_json(
            f"content/{content_id}",
            params={"expand": CONTENT_EXPAND},
        )

    def _fetch_comments(self, content_id: str) -> list[dict[str, Any]]:
        """Fetch all comments for one content item while enforcing configured caps."""
        comments: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.client.get_json(
                f"content/{content_id}/child/comment",
                params={
                    "depth": "all",
                    "expand": COMMENT_EXPAND,
                    "limit": self.config.page_size,
                    "start": start,
                },
            )
            results = response.get("results", [])
            extend_with_limit(
                comments,
                results,
                limit=self.config.max_comments,
                label=f"Confluence comments for {content_id}",
                setting_name="max_comments",
            )
            if len(results) < self.config.page_size:
                return comments
            start += len(results)

    def _list_attachments(self, content_id: str) -> list[dict[str, Any]]:
        """Fetch attachment metadata for one content item within configured caps."""
        attachments: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.client.get_json(
                f"content/{content_id}/child/attachment",
                params={"limit": self.config.page_size, "start": start},
            )
            results = response.get("results", [])
            extend_with_limit(
                attachments,
                results,
                limit=self.config.max_attachments,
                label=f"Confluence attachments for {content_id}",
                setting_name="max_attachments",
            )
            if len(results) < self.config.page_size:
                return attachments
            start += len(results)

    def _with_children(
        self,
        content_ids: list[str],
        query: ConnectorQuery,
    ) -> Iterator[str]:
        """Yield requested content IDs and optionally traverse child pages."""
        include_children = bool(query.filters.get("include_children"))
        seen: set[str] = set()
        for content_id in content_ids:
            if content_id in seen:
                continue
            seen.add(content_id)
            yield content_id
            if include_children:
                yield from self._child_page_ids(
                    content_id,
                    recursive=query.recursive,
                    seen=seen,
                )

    def _child_page_ids(
        self,
        content_id: str,
        *,
        recursive: bool,
        seen: set[str],
    ) -> Iterator[str]:
        """Traverse child page IDs without revisiting already emitted pages."""
        start = 0
        while True:
            response = self.client.get_json(
                f"content/{content_id}/child/page",
                params={"limit": self.config.page_size, "start": start},
            )
            results = response.get("results", [])
            for child in results:
                child_id = str(child.get("id") or "")
                if not child_id or child_id in seen:
                    continue
                seen.add(child_id)
                yield child_id
                if recursive:
                    yield from self._child_page_ids(
                        child_id,
                        recursive=True,
                        seen=seen,
                    )
            if len(results) < self.config.page_size:
                return
            start += len(results)

    def _cql_from_query(self, query: ConnectorQuery) -> str:
        """Translate shared connector filters into Confluence CQL."""
        filters = query.filters
        space_key = str(
            filters.get("space_key") or query.path or self.config.space_key
        )
        return build_cql(
            space_key=space_key,
            content_types=self._list_filter(
                filters.get("content_types"),
                default=self.config.content_types,
            ),
            labels=self._list_filter(
                filters.get("labels") or filters.get("label"),
                default=self.config.include_labels,
            ),
            updated_after=query.updated_after,
            raw_cql=filters.get("cql") or query.pattern,
        )

    @staticmethod
    def _content_ids_from_query(query: ConnectorQuery) -> list[str]:
        values = query.filters.get("content_ids") or query.filters.get("page_ids")
        if values is None:
            return []
        if isinstance(values, str):
            return [values]
        return [str(value) for value in values]

    @staticmethod
    def _list_filter(value: Any, *, default: list[str]) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    def _record_for_id(self, content_id: str, query: ConnectorQuery) -> SourceRecord:
        """Build a direct-load record when discovery is driven by explicit IDs."""
        return SourceRecord(
            id=f"confluence://{self.config.space_key}/{content_id}",
            source_type="text/html",
            locator=str(content_id),
            metadata={
                "content_id": str(content_id),
                "space_key": self.config.space_key,
                "include_attachments": query.include_attachments,
            },
        )

    def _should_process_content(self, content: dict[str, Any]) -> bool:
        """Apply include/exclude label filters to Confluence content."""
        labels = content.get("metadata", {}).get("labels", {}).get("results", [])
        label_names = {
            str(label.get("name")) for label in labels if isinstance(label, dict)
        }
        if self.config.exclude_labels and label_names.intersection(
            self.config.exclude_labels
        ):
            return False
        if self.config.include_labels:
            return bool(label_names.intersection(self.config.include_labels))
        return True

    @staticmethod
    def _validate_content(content: dict[str, Any], content_id: str) -> None:
        """Fail fast when Confluence omits fields required by mappers."""
        missing = [
            name
            for name, value in (
                ("id", content.get("id")),
                ("title", content.get("title")),
                ("space.key", content.get("space", {}).get("key")),
            )
            if not value
        ]
        if missing:
            raise DocumentProcessingError(
                f"Confluence content {content_id} missing required fields: "
                f"{', '.join(missing)}"
            )


class _RequestsConfluenceClient:
    """Authenticated, rate-limited Confluence REST client."""

    def __init__(self, config: ConfluenceSpaceConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if config.deployment_type == ConfluenceDeploymentType.CLOUD:
            self.session.auth = (config.email, config.token)
        else:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a Confluence REST endpoint and decode its JSON body."""
        response = self._request(
            "GET",
            self._api_url(endpoint),
            params=params,
        )
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"Confluence returned non-JSON response for {endpoint}") from exc

    def download_bytes(self, url: str) -> bytes | None:
        """Download attachment bytes only from the configured Confluence origin."""
        try:
            safe_url = require_same_origin_url(
                url,
                self.base_url,
                label="Confluence download",
            )
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        response = self._request("GET", safe_url, headers={"Accept": "*/*"})
        return response.content or None

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one HTTP request with local rate limiting and retry handling."""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._acquire()
            try:
                response = self.session.request(
                    method,
                    url,
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
                        f"Confluence request failed with HTTP "
                        f"{response.status_code}: {response.text}"
                    )
                return response

            last_error = FetchError(
                f"Confluence request returned HTTP {response.status_code}"
            )
            self._sleep(attempt, last_error, response.headers)

        raise FetchError(str(last_error))

    def _api_url(self, endpoint: str) -> str:
        """Build a Confluence REST API URL from a relative endpoint."""
        return f"{self.base_url}/rest/api/{endpoint.lstrip('/')}"

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
            "Retrying Confluence request after error, attempt %d/%d: %s",
            attempt + 1,
            self.config.max_retries,
            error,
        )
        if delay > 0:
            time.sleep(delay)
