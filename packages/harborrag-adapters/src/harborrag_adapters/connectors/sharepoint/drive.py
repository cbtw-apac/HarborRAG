"""SharePoint site, drive, and item traversal operations."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery

from .client import SharePointClient
from .config import SharePointSiteConfig
from .mappers import build_source_record, parse_timestamp
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


class SharePointDriveAPI:
    """SharePoint site, drive, and drive-item traversal helpers."""

    def __init__(self, client: SharePointClient, config: SharePointSiteConfig) -> None:
        """Bind drive traversal to a client and validated config."""
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
        """Return records for a direct drive item ID, descending into folders."""
        item = self.get_item(str(drive["id"]), item_id)
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
        """Walk child items breadth-first enough to yield files before recursion."""
        drive_id = str(drive["id"])
        folder_ids: list[str] = []
        for item in self.iter_children(drive_id, item_id=item_id, path=path):
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
                if child_id:
                    folder_ids.append(child_id)

        for child_id in folder_ids:
            yield from self.walk_children(
                site=site,
                drive=drive,
                query=query,
                item_id=child_id,
            )

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

        while endpoint:
            response = self.client.get_json(endpoint, params=params)
            yield from response.get("value", [])
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

        while endpoint:
            response = self.client.get_json(endpoint, params=params)
            yield from response.get("value", [])
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
        site = self.client.get_json(endpoint)
        if not site.get("id"):
            raise FetchError("SharePoint site response did not include id")
        self._site = site
        logger.debug("Resolved SharePoint site %s", site.get("id"))
        return site

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

    def should_process_file(self, item: dict[str, Any], query: ConnectorQuery) -> bool:
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

    def enforce_size_limit(self, item: dict[str, Any]) -> None:
        """Prevent large drive files from being downloaded by direct loads."""
        size = int(item.get("size") or 0)
        if self.config.max_file_size_bytes is None or not size:
            return
        if size > self.config.max_file_size_bytes:
            raise DocumentProcessingError(
                f"SharePoint file {item_name(item)!r} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}"
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
