"""Structured metadata emitted by the SharePoint connector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from harborrag_adapters.connectors.schemas import ConnectorMetadata


@dataclass(slots=True)
class SharePointParentReference:
    """Parent drive item reference from Microsoft Graph."""

    drive_id: Any
    id: Any
    path: Any


@dataclass(slots=True, kw_only=True)
class SharePointMetadata(ConnectorMetadata):
    """Structured metadata for one loaded SharePoint drive item."""

    source_system: ClassVar[str] = "sharepoint"

    site_id: Any
    site_name: Any
    drive_id: Any
    drive_name: Any
    drive_type: Any
    item_id: Any
    item_name: str
    path: str
    size: int
    etag: Any
    ctag: Any
    created_by: str | None
    updated_by: str | None
    parent: SharePointParentReference
    sharepoint_hashes: dict[str, Any]


@dataclass(slots=True, kw_only=True)
class SharePointPageMetadata(ConnectorMetadata):
    """Structured metadata for one loaded SharePoint modern site page."""

    source_system: ClassVar[str] = "sharepoint"

    record_kind: str = "page"
    site_id: Any
    site_name: Any
    page_id: str
    item_name: str
    page_layout: Any
    etag: Any
    created_by: str | None
    updated_by: str | None
    has_text_content: bool
