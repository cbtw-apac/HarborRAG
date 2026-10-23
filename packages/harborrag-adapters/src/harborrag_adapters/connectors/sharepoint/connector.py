"""SharePoint document-library connector.

Handles:
- Discovery from a drive walk or explicit item ids
- Per-item permission classification and restricted-content gating
- File download with drive and root confinement
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from itertools import islice
from typing import Any

from harborrag_adapters.connectors.acl import RestrictedContentGate, acl_metadata
from harborrag_adapters.connectors.base import BaseConnector, replay_discovery_page
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorPage,
    ConnectorQuery,
    ConnectorSkip,
)
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .client import SharePointClient, _RequestsGraphClient
from .config import SharePointSiteConfig
from .drive import SharePointDriveAPI
from .drive_paths import is_drive_file, item_mime_type, item_name, scoped_drive_root
from .mappers import build_document_metadata, drive_item_id_from_record
from .permissions import SharePointPermissionResolver, site_realm

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")
_TIME_FOR_SHAREPOINT_CONNECTOR_TESTS = time


class SharePointConnector(BaseConnector):
    """Connector for SharePoint document libraries over Microsoft Graph.

    Caches item permissions per scan; ``discover`` and a first ``discover_page`` reset them.
    """

    provider_name = "sharepoint"
    # TODO: advertise ``pagination=False`` or implement ``discover_page`` natively.
    capabilities = ConnectorCapabilities(
        pagination=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
        permissions=True,
    )

    def __init__(
        self,
        config: SharePointSiteConfig,
        *,
        client: SharePointClient | None = None,
    ) -> None:
        """Create the drive API, permission resolver and access gate.

        Args:
            config: Validated site config.
            client: Client to use instead of the default Graph client.
        """
        self.config = config
        self.client = client or _RequestsGraphClient(config)
        self._drive = SharePointDriveAPI(self.client, config)
        self._permissions = SharePointPermissionResolver(self.client, realm=site_realm(config))
        self._gate = RestrictedContentGate(config.restricted_content)

    def close(self) -> None:
        """Close the Graph client."""

        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return the files the latest discovery withheld because of source access."""

        return self._gate.skipped

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield file records from explicit item ids or a drive walk, as a new scan.

        Cached permissions are dropped first. Files the restricted-content policy
        withholds are skipped and reported through ``skipped``.

        Args:
            query: Path, pattern, filter and limit selectors.

        Yields:
            One ``SourceRecord`` per admitted file, with ACL evidence in its metadata.
        """
        self._permissions.reset()
        return self._discover_records(query)

    def discover_page(
        self,
        query: ConnectorQuery | None,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ConnectorPage:
        """Serve one page by replaying discovery, reusing the scan's cached permissions.

        ``cursor=None`` starts a new scan and drops cached permissions.

        Args:
            query: Path, pattern, filter and limit selectors.
            cursor: Numeric offset from a previous page, or ``None`` for the first page.
            page_size: Maximum records in the page.

        Returns:
            Page of records with the next cursor, or ``None`` on the last page.
        """
        if cursor is None:
            self._permissions.reset()
        return replay_discovery_page(
            self._discover_records, query, cursor=cursor, page_size=page_size
        )

    def _discover_records(self, query: ConnectorQuery | None) -> Iterator[SourceRecord]:
        """Yield admitted records for one scan, resetting the gate's skip list first.

        Withheld files do not count toward ``query.limit``.
        """
        query = query or ConnectorQuery()
        self._gate.reset()
        site = self._drive.resolve_site()
        drive = self._drive.resolve_drive(site)
        logger.info(
            "Discovering SharePoint files in site=%s drive=%s",
            site.get("id"),
            drive.get("id"),
        )
        candidates = self._candidates(query, site=site, drive=drive)
        yield from islice(filter(self._admit, candidates), query.limit)

    def _candidates(
        self,
        query: ConnectorQuery,
        *,
        site: dict[str, Any],
        drive: dict[str, Any],
    ) -> Iterator[SourceRecord]:
        """Yield file records from explicit item ids, else by walking the drive from a root.

        Explicit ids take precedence over the query path and ``root_item_id``; every item
        is still checked against ``root_path``.
        """
        item_ids = self._item_ids_from_query(query)
        if item_ids:
            for item_id in item_ids:
                yield from self._drive.records_from_item_id(
                    item_id, site=site, drive=drive, query=query
                )
            return
        root_path = scoped_drive_root(self.config.root_path, query.path)
        root_item_id = self._root_item_id_from_query(query)
        if root_item_id and self.config.root_path:
            self._drive.require_within_root(
                self._drive.get_item(str(drive["id"]), root_item_id), root_item_id
            )
        yield from self._drive.walk_children(
            site=site,
            drive=drive,
            query=query,
            item_id=root_item_id,
            path=None if root_item_id else root_path,
        )

    def _site_key(self) -> str:
        """Return the resolved site id."""

        return str(self._drive.resolve_site()["id"])

    def _admit(self, record: SourceRecord) -> bool:
        """Return whether the gate admits the record, adding its ACL evidence if so."""
        acl = self._permissions.item_acl(
            str(record.metadata["drive_id"]),
            str(record.metadata["item_id"]),
            site_key=self._site_key(),
            version=record.metadata.get("ctag"),
        )
        if not self._gate.admit(acl, path=str(record.metadata.get("path") or record.locator)):
            return False
        record.metadata.update(acl_metadata(acl))
        return True

    def load(self, record: SourceRecord) -> RawDocument:
        """Download one drive file as a raw document.

        Fails closed on files the restricted-content policy withholds.

        Args:
            record: File record produced by discovery.

        Returns:
            Raw document with the file bytes, drive metadata and ACL evidence.

        Raises:
            DocumentProcessingError: If the item is in another drive, is not a file, is outside
                ``root_path``, or exceeds ``max_file_size_bytes``.
            SourceRestrictedError: If restricted-content policy withholds the file.
        """
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
        self._drive.require_within_root(item, item_id)
        self._drive.enforce_size_limit(item)
        acl = self._permissions.item_acl(drive_id, item_id, site_key=str(site["id"]))
        self._gate.require(acl, path=item_id)

        logger.info(
            "Loading SharePoint file item=%s drive=%s name=%s",
            item_id,
            drive_id,
            item_name(item),
        )
        # TODO: URL-encode item ids in Graph endpoints.
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
            metadata={**metadata.to_dict(), **acl_metadata(acl)},
            raw=item,
        )

    def load_by_ids(self, item_ids: list[str]) -> Iterator[RawDocument]:
        """Yield raw documents for known drive item ids.

        Args:
            item_ids: Drive item ids.
        """
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
