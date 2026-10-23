"""SharePoint site, drive and item operations over Microsoft Graph.

Handles:
- Site and drive resolution
- Paged, cycle-checked folder traversal
- File filters, size limit and root confinement
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

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
    item_within_root,
    matches_pattern,
    site_path_endpoint,
)
from .mappers import build_source_record, parse_timestamp

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")
_MAX_PROVIDER_PAGES = 10_000


class SharePointDriveAPI:
    """Graph operations for one SharePoint site and drive.

    Caches the resolved site and drive for the lifetime of the instance.
    """

    def __init__(self, client: SharePointClient, config: SharePointSiteConfig) -> None:
        """Bind the drive API to a client.

        Args:
            client: Graph client used for all requests.
            config: Validated site config.
        """
        self.client = client
        self.config = config
        self._site: dict[str, Any] | None = None
        self._drive: dict[str, Any] | None = None

    def records_from_item_id(
        self,
        item_id: str,
        *,
        site: dict[str, Any],
        drive: dict[str, Any],
        query: ConnectorQuery,
    ) -> Iterator[SourceRecord]:
        """Yield records for a drive item id, descending into folders.

        Args:
            item_id: Drive item id of a file or folder.
            site: Resolved site payload.
            drive: Resolved drive payload.
            query: Discovery query.

        Raises:
            DocumentProcessingError: If the item is outside ``root_path``.
        """
        item = self.get_item(str(drive["id"]), item_id)
        self.require_within_root(item, item_id)
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
        """Yield file records under a folder depth-first, using an explicit stack.

        Subfolders are descended only when ``query.recursive`` is set.

        Args:
            site: Resolved site payload.
            drive: Resolved drive payload.
            query: Discovery query.
            item_id: Folder item id to start from.
            path: Folder path to start from when ``item_id`` is unset.

        Raises:
            FetchError: If the folder tree contains a cycle.
        """
        drive_id = str(drive["id"])
        pending: list[tuple[str | None, str | None]] = [(item_id, path)]
        seen_folder_ids = {item_id} if item_id else set()
        while pending:
            current_item_id, current_path = pending.pop()
            folder_ids: list[str] = []
            for item in self._children_within_root(
                drive_id, item_id=current_item_id, path=current_path
            ):
                if is_drive_file(item):
                    if self.should_process_file(item, query):
                        yield build_source_record(
                            item,
                            site_id=str(site["id"]),
                            drive_id=drive_id,
                        )
                    continue

                if query.recursive and is_drive_folder(item):
                    child_id = str(item.get("id") or "")
                    if not child_id:
                        continue
                    if child_id in seen_folder_ids:
                        raise FetchError("SharePoint folder traversal contains a cycle")
                    seen_folder_ids.add(child_id)
                    folder_ids.append(child_id)

            for child_id in reversed(folder_ids):
                pending.append((child_id, None))

    def _children_within_root(
        self,
        drive_id: str,
        *,
        item_id: str | None,
        path: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield children under ``root_path``, logging one warning per folder for the rest."""
        skipped = 0
        for item in self.iter_children(drive_id, item_id=item_id, path=path):
            if item_within_root(item, self.config.root_path):
                yield item
            else:
                skipped += 1
        if skipped:
            logger.warning(
                "Skipped %d SharePoint items outside root_path (or without a parent path)",
                skipped,
            )

    def iter_children(
        self,
        drive_id: str,
        *,
        item_id: str | None = None,
        path: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield the children of a folder item or path across all Graph pages.

        Raises:
            FetchError: If pagination does not advance or exceeds the page cap.
        """
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
        """Yield the drives of a site across all Graph pages.

        Raises:
            FetchError: If pagination does not advance or exceeds the page cap.
        """
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
        """Return one drive item with the fields mappers and filters use."""
        # TODO: URL-encode item ids in Graph endpoints.
        return self.client.get_json(
            f"drives/{drive_id}/items/{item_id}",
            params={"$select": DRIVE_ITEM_SELECT},
        )

    def resolve_site(self) -> dict[str, Any]:
        """Return the configured site, resolving it once.

        Raises:
            FetchError: If the site response lacks an id.
        """
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

    def resolve_drive(self, site: dict[str, Any]) -> dict[str, Any]:
        """Return the configured or default document library drive, resolving it once.

        Args:
            site: Resolved site payload.

        Raises:
            FetchError: If the named drive is not found or the response lacks an id.
        """
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

    def should_process_file(self, item: dict[str, Any], query: ConnectorQuery) -> bool:
        """Return whether a file item passes the config and query filters."""
        name = item_name(item)
        mime_type = item_mime_type(item)
        size = int(item.get("size") or 0)
        extension = item_extension(item)

        if not self._matches_file_policy(item, query, name=name, size=size, extension=extension):
            return False
        return self._callback_allows_file(name, size, mime_type)

    def _matches_file_policy(
        self,
        item: dict[str, Any],
        query: ConnectorQuery,
        *,
        name: str,
        size: int,
        extension: str,
    ) -> bool:
        """Apply the hidden, size, extension, modified-time and name-pattern rules in order."""
        if item_hidden(item) and not self.config.include_hidden:
            logger.debug("Skipping hidden SharePoint file %s", name)
            return False
        limit = self.config.max_file_size_bytes
        if limit is not None and size > limit:
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
        return matches_pattern(item, query.pattern)

    def _callback_allows_file(self, name: str, size: int, mime_type: str) -> bool:
        """Ask ``process_file_callback``. A failing callback skips unless ``fail_on_error``."""
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
        """Raise ``DocumentProcessingError`` if the item exceeds ``max_file_size_bytes``."""
        size = int(item.get("size") or 0)
        if self.config.max_file_size_bytes is None or not size:
            return
        if size > self.config.max_file_size_bytes:
            raise DocumentProcessingError(
                f"SharePoint file {item_name(item)!r} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}"
            )

    def require_within_root(self, item: dict[str, Any], item_id: str) -> None:
        """Raise ``DocumentProcessingError`` if the item is outside ``root_path``.

        The message contains only the caller-supplied id.
        """
        if not item_within_root(item, self.config.root_path):
            raise DocumentProcessingError(
                f"SharePoint item {item_id!r} is outside the configured root_path"
            )

    def record_for_item_id(self, item_id: str) -> SourceRecord:
        """Return the source record for a drive item id."""
        site = self.resolve_site()
        drive = self.resolve_drive(site)
        item = self.get_item(str(drive["id"]), item_id)
        return build_source_record(
            item,
            site_id=str(site["id"]),
            drive_id=str(drive["id"]),
        )


def _page_items(response: dict[str, Any], *, operation: str) -> list[dict[str, Any]]:
    """Return the page's ``value`` list. Raises ``FetchError`` if it is not a list of objects."""
    raw_items = response.get("value", [])
    if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
        raise FetchError(f"SharePoint {operation} response contained an invalid value list")
    return raw_items
