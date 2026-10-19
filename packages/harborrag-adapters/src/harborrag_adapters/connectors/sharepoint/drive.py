"""SharePoint site, drive, and item traversal operations."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.exceptions import AuthorizationError, FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

from .allowlist import SharePointAllowlist
from .client import SharePointClient
from .config import SharePointSiteConfig
from .drive_paths import (
    DRIVE_ITEM_SELECT,
    DRIVE_SELECT,
    children_endpoint,
    is_drive_file,
    is_drive_folder,
    item_extension,
    item_hidden,
    item_mime_type,
    item_name,
    item_path,
    matches_pattern,
    site_path_endpoint,
)
from .mappers import build_source_record, parse_timestamp
from .skips import (
    EXCEEDS_SIZE_LIMIT,
    NOT_IN_ALLOWLIST,
    UNSUPPORTED_EXTENSION,
    SharePointItemSkippedError,
    SharePointSkipRecorder,
    skip_reason_for_error,
)

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")
_MAX_PROVIDER_PAGES = 10_000
PAGE_FILE_EXTENSION = ".aspx"


class SharePointDriveAPI:
    """SharePoint site, drive, and drive-item traversal helpers."""

    def __init__(
        self,
        client: SharePointClient,
        config: SharePointSiteConfig,
        *,
        skips: SharePointSkipRecorder | None = None,
    ) -> None:
        """Bind drive traversal to a client, validated config, and skip recorder."""
        self.client = client
        self.config = config
        self.skips = skips or SharePointSkipRecorder()
        self.allowlist = SharePointAllowlist(config)
        self._site: dict[str, Any] | None = None
        self._drive: dict[str, Any] | None = None
        self._drives: list[dict[str, Any]] | None = None

    def records_from_item_id(
        self,
        item_id: str,
        *,
        site: dict[str, Any],
        drive: dict[str, Any],
        query: ConnectorQuery,
    ) -> Iterator[SourceRecord]:
        """Return records for a direct drive item ID, descending into folders.

        A direct item that is gone, forbidden, or still failing after retries
        is recorded as a skip so the other requested items still load.
        """
        try:
            item = self.get_item(str(drive["id"]), item_id)
        except (AuthorizationError, FetchError) as exc:
            reason = skip_reason_for_error(exc)
            if reason is None:
                raise
            self.skips.record(reason, str(exc), item_id=item_id)
            return
        if is_drive_file(item):
            if self.should_process_file(item, query):
                yield build_source_record(
                    item,
                    site_id=str(site["id"]),
                    drive_id=str(drive["id"]),
                )
            return

        if is_drive_folder(item):
            yield from self.walk_children(
                site=site,
                drive=drive,
                query=query,
                item_id=item_id,
            )

    def walk_children(
        self,
        *,
        site: dict[str, Any],
        drive: dict[str, Any],
        query: ConnectorQuery,
        item_id: str | None = None,
        path: str | None = None,
    ) -> Iterator[SourceRecord]:
        """Walk child items depth-first with an explicit stack.

        A folder tree with no depth cap and no cycle protection previously
        recursed once per subfolder, so an extremely deep tree could grow the
        Python call stack past its limit before ``query.limit`` ever has a
        chance to stop discovery. An explicit stack removes that ceiling
        while preserving the same depth-first yield order.
        """
        drive_id = str(drive["id"])
        pending: list[tuple[str | None, str | None]] = [(item_id, path)]
        seen_folder_ids = {item_id} if item_id else set()
        while pending:
            current_item_id, current_path = pending.pop()
            folder_ids: list[str] = []
            for item in self.iter_children(drive_id, item_id=current_item_id, path=current_path):
                if is_drive_file(item):
                    if self.should_process_file(item, query):
                        yield build_source_record(
                            item,
                            site_id=str(site["id"]),
                            drive_id=drive_id,
                        )
                    continue

                child_id = self._folder_to_descend(item, query, seen_folder_ids)
                if child_id:
                    folder_ids.append(child_id)

            for child_id in reversed(folder_ids):
                pending.append((child_id, None))

    def _folder_to_descend(
        self,
        item: dict[str, Any],
        query: ConnectorQuery,
        seen_folder_ids: set[str],
    ) -> str | None:
        """Return a child folder ID to walk, or ``None`` when it is out of scope.

        A folder that cannot hold anything under an allowed path prefix is not
        listed at all, so nothing beneath it is fetched.
        """
        if not (query.recursive and self.config.recursive and is_drive_folder(item)):
            return None
        child_id = str(item.get("id") or "")
        if not child_id:
            return None
        if not self.allowlist.folder_may_contain_allowed(item_path(item)):
            logger.debug("Skipping SharePoint folder outside allowed paths %s", item_name(item))
            return None
        if child_id in seen_folder_ids:
            raise FetchError("SharePoint folder traversal contains a cycle")
        seen_folder_ids.add(child_id)
        return child_id

    def iter_children(
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
        seen_endpoints: set[str] = set()

        while endpoint:
            if endpoint in seen_endpoints or len(seen_endpoints) >= _MAX_PROVIDER_PAGES:
                raise FetchError("SharePoint children pagination did not advance")
            seen_endpoints.add(endpoint)
            response = self.client.get_json(endpoint, params=params)
            yield from _page_items(response, operation="children")
            next_link = response.get("@odata.nextLink")
            endpoint = str(next_link) if next_link else ""
            params = None

    def iter_site_drives(self, site_id: str) -> Iterator[dict[str, Any]]:
        """Iterate drives for a site when resolving a configured drive name."""
        endpoint = f"sites/{site_id}/drives"
        params: dict[str, Any] | None = {
            "$top": self.config.page_size,
            "$select": DRIVE_SELECT,
        }
        seen_endpoints: set[str] = set()

        while endpoint:
            if endpoint in seen_endpoints or len(seen_endpoints) >= _MAX_PROVIDER_PAGES:
                raise FetchError("SharePoint drive pagination did not advance")
            seen_endpoints.add(endpoint)
            response = self.client.get_json(endpoint, params=params)
            yield from _page_items(response, operation="drives")
            next_link = response.get("@odata.nextLink")
            endpoint = str(next_link) if next_link else ""
            params = None

    def get_item(self, drive_id: str, item_id: str) -> dict[str, Any]:
        """Fetch one drive item with the fields used by mappers and filters."""
        return self.client.get_json(
            f"drives/{drive_id}/items/{item_id}",
            params={"$select": DRIVE_ITEM_SELECT},
        )

    def resolve_site(self) -> dict[str, Any]:
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
        try:
            site = self.client.get_json(endpoint)
        except AuthorizationError as exc:
            # The configured site itself is off limits, so nothing in scope is
            # reachable: this is fatal for the run, not a per-item skip.
            raise AuthorizationError(
                f"SharePoint app is not granted access to site {self.config.site_url or endpoint!r}; "
                "grant it Sites.Selected access to this site",
                status_code=403,
            ) from exc
        if not site.get("id"):
            raise FetchError("SharePoint site response did not include id")
        self._site = site
        logger.debug("Resolved SharePoint site %s", site.get("id"))
        return site

    def verify_site_access(self, site: dict[str, Any]) -> None:
        """Read a site configured by ``site_id`` once, so auth fails at startup.

        A site resolved from its URL was already read from Graph. One given by
        ID was not, so without this check bad credentials or a missing
        ``Sites.Selected`` grant would only surface mid-traversal. The site's
        ``webUrl`` is kept when the config did not supply one, which lets a
        URL entry in ``allowed_sites`` match a site configured by ID.
        """
        if not self.config.site_id:
            return
        endpoint = f"sites/{self.config.site_id}"
        try:
            remote = self.client.get_json(endpoint, params={"$select": "id,webUrl,displayName"})
        except AuthorizationError as exc:
            raise AuthorizationError(
                f"SharePoint app is not granted access to site {self.config.site_id!r}; "
                "grant it Sites.Selected access to this site",
                status_code=403,
            ) from exc
        if not site.get("webUrl") and remote.get("webUrl"):
            site["webUrl"] = remote["webUrl"]

    def resolve_drive(self, site: dict[str, Any]) -> dict[str, Any]:
        """Resolve and cache the configured or default document library drive."""
        if self._drive is not None:
            return self._drive

        if self.config.drive_id:
            self._drive = {"id": self.config.drive_id, "name": self.config.drive_name}
            return self._drive

        site_id = str(site["id"])
        if self.config.drive_name:
            expected = self.config.drive_name.casefold()
            for drive in self.iter_site_drives(site_id):
                if str(drive.get("name") or "").casefold() == expected:
                    self._drive = drive
                    logger.debug("Resolved SharePoint drive %s", drive.get("id"))
                    return drive
            raise FetchError(f"SharePoint drive named {self.config.drive_name!r} was not found")

        drive = self.client.get_json(
            f"sites/{site_id}/drive",
            params={"$select": DRIVE_SELECT},
        )
        if not drive.get("id"):
            raise FetchError("SharePoint default drive response did not include id")
        self._drive = drive
        logger.debug("Resolved SharePoint default drive %s", drive.get("id"))
        return drive

    def resolve_drives(self, site: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve the drives in crawl scope: every document library, or one drive."""
        if not self.config.crawl_all_drives:
            return [self.resolve_drive(site)]
        if self._drives is None:
            self._drives = [
                drive
                for drive in self.iter_site_drives(str(site["id"]))
                if drive.get("id")
                and drive.get("driveType", "documentLibrary") == "documentLibrary"
            ]
            logger.debug("Resolved %d SharePoint document libraries", len(self._drives))
        return self._drives

    def drive_in_scope(self, site: dict[str, Any], drive_id: str) -> dict[str, Any] | None:
        """Return the in-scope drive with this ID, or ``None`` when it is outside."""
        for drive in self.resolve_drives(site):
            if str(drive.get("id")) == drive_id:
                return drive
        return None

    def should_process_file(self, item: dict[str, Any], query: ConnectorQuery) -> bool:
        """Apply query/config filters to one Microsoft Graph drive item.

        Query filters (``updated_after``, ``pattern``) and hidden files are
        intended filtering, not skips. An item declined for an allowlist, size,
        or extension reason is recorded as a skip with that reason.
        """
        name = item_name(item)
        if not self._matches_query(item, query, name=name):
            return False
        declined = self.declined_reason(item)
        if declined is not None:
            reason, detail = declined
            self.skips.record(
                reason,
                detail,
                item_id=str(item.get("id") or "") or None,
                url=item.get("webUrl"),
                path=item_path(item),
            )
            return False
        return self._callback_allows_file(name, int(item.get("size") or 0), item_mime_type(item))

    def _matches_query(self, item: dict[str, Any], query: ConnectorQuery, *, name: str) -> bool:
        if self.config.include_pages and item_extension(item) == PAGE_FILE_EXTENSION:
            # A modern page is also an .aspx file in the Site Pages library. With
            # pages on, the pages API owns it, so the drive walk must not ingest
            # the same page a second time under a file identity.
            logger.debug("Skipping SharePoint page file %s; pages API loads it", name)
            return False
        if item_hidden(item) and not self.config.include_hidden:
            logger.debug("Skipping hidden SharePoint file %s", name)
            return False
        if query.updated_after:
            updated_at = parse_timestamp(item.get("lastModifiedDateTime"))
            if updated_at and updated_at <= query.updated_after:
                return False
        return matches_pattern(item, query.pattern)

    def declined_reason(self, item: dict[str, Any]) -> tuple[str, str] | None:
        """Return the standard skip reason and detail for an out-of-policy file."""
        path = item_path(item)
        if not self.allowlist.path_allowed(path):
            return NOT_IN_ALLOWLIST, f"path {path!r} is outside allowed_path_prefixes"
        size = int(item.get("size") or 0)
        limit = self.config.max_file_size_bytes
        if limit is not None and size > limit:
            return EXCEEDS_SIZE_LIMIT, f"size {size} exceeds max_file_size_bytes {limit}"
        extension = item_extension(item)
        if extension not in self.config.allowed_extensions:
            shown = extension or "<none>"
            return UNSUPPORTED_EXTENSION, f"extension {shown!r} is not allowed"
        if extension in self.config.excluded_extensions:
            return UNSUPPORTED_EXTENSION, f"extension {extension!r} is excluded"
        return None

    def _callback_allows_file(self, name: str, size: int, mime_type: str) -> bool:
        callback = self.config.process_file_callback
        if callback is None:
            return True
        try:
            should_process, reason = callback(name, size, mime_type)
        except Exception:
            if self.config.fail_on_error:
                raise
            logger.exception("SharePoint file callback failed for %s", name)
            return False
        if not should_process:
            logger.debug("Skipping SharePoint file %s: %s", name, reason)
            return False
        return True

    def enforce_size_limit(self, item: dict[str, Any]) -> None:
        """Prevent large drive files from being downloaded by direct loads."""
        size = int(item.get("size") or 0)
        if self.config.max_file_size_bytes is None or not size:
            return
        if size > self.config.max_file_size_bytes:
            raise SharePointItemSkippedError(
                EXCEEDS_SIZE_LIMIT,
                f"SharePoint file {item_name(item)!r} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}",
                item_id=str(item.get("id") or "") or None,
                url=item.get("webUrl"),
            )

    def record_for_item_id(self, item_id: str) -> SourceRecord:
        """Resolve a drive item ID and build its source record."""
        site = self.resolve_site()
        drive = self.resolve_drive(site)
        item = self.get_item(str(drive["id"]), item_id)
        return build_source_record(
            item,
            site_id=str(site["id"]),
            drive_id=str(drive["id"]),
        )


def _page_items(response: dict[str, Any], *, operation: str) -> list[dict[str, Any]]:
    """Validate Graph collection pages before exposing provider-controlled data."""
    raw_items = response.get("value", [])
    if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
        raise FetchError(f"SharePoint {operation} response contained an invalid value list")
    return raw_items
