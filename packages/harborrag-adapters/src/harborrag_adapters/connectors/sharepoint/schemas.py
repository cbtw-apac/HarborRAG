"""Metadata types emitted by the SharePoint connector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from harborrag_adapters.connectors.schemas import ConnectorMetadata


@dataclass(slots=True)
class SharePointParentReference:
    """Parent drive item reference from Graph."""

    drive_id: Any
    id: Any
    path: Any


@dataclass(slots=True, kw_only=True)
class SharePointMetadata(ConnectorMetadata):
    """Metadata for one loaded SharePoint drive item.

    Attributes:
        etag: Graph ``eTag`` of the item.
        ctag: Graph ``cTag`` of the item.
        sharepoint_hashes: Graph file hashes, such as ``quickXorHash``.
    """

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
