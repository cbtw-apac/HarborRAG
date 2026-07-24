"""SharePoint drive discovery and raw-file loading orchestration."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .client import SharePointClient, _RequestsGraphClient
from .config import SharePointSiteConfig
from .drive import SharePointDriveAPI
from .drive_paths import is_drive_file, item_mime_type, item_name
from .mappers import build_document_metadata, drive_item_id_from_record

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")
_TIME_FOR_SHAREPOINT_CONNECTOR_TESTS = time


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
        """Initialize drive operations with an optional client override."""
        self.config = config
        self.client = client or _RequestsGraphClient(config)
        self._drive = SharePointDriveAPI(self.client, config)

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Discover SharePoint drive-item records from IDs or folder traversal."""
        query = query or ConnectorQuery()
        site = self._drive.resolve_site()
        drive = self._drive.resolve_drive(site)
        logger.info(
            "Discovering SharePoint files in site=%s drive=%s",
            site.get("id"),
            drive.get("id"),
        )

        yielded = 0
        item_ids = self._item_ids_from_query(query)
        if item_ids:
            for item_id in item_ids:
                for record in self._drive.records_from_item_id(
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
        for record in self._drive.walk_children(
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
        site = self._drive.resolve_site()
        drive = self._drive.resolve_drive(site)
        expected_drive_id = str(drive.get("id"))
        item_id = drive_item_id_from_record(record)
        record_drive_id = record.metadata.get("drive_id")
        drive_id = str(record_drive_id) if record_drive_id is not None else expected_drive_id
        if drive_id != expected_drive_id:
            raise DocumentProcessingError(
                f"SharePoint item {item_id} belongs to drive {drive_id!r}, "
                f"outside configured drive {expected_drive_id!r}"
            )
        item = self._drive.get_item(drive_id, item_id)

        if not is_drive_file(item):
            raise DocumentProcessingError(
                f"SharePoint drive item {item_id} is not a downloadable file"
            )
        self._drive.enforce_size_limit(item)

        logger.info(
            "Loading SharePoint file item=%s drive=%s name=%s",
            item_id,
            drive_id,
            item_name(item),
        )
        content = self.client.get_bytes(f"drives/{drive_id}/items/{item_id}/content")
        metadata = build_document_metadata(
            item,
            site=site,
            drive=drive,
            checksum=hashlib.sha256(content).hexdigest(),
        )

        return RawDocument(
            id=record.id,
            source=str(item.get("webUrl") or item_id),
            content=content,
            content_type=item_mime_type(item),
            metadata=metadata.to_dict(),
            raw=item,
        )

    def load_by_ids(self, item_ids: list[str]) -> Iterator[RawDocument]:
        """Load files for callers that already have drive item IDs."""
        for item_id in item_ids:
            yield self.load(self._drive.record_for_item_id(item_id))

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
