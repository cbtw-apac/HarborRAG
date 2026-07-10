"""Structured metadata emitted by the SharePoint connector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SharePointParentReference:
    """Parent drive item reference from Microsoft Graph."""

    drive_id: Any
    id: Any
    path: Any


@dataclass(slots=True)
class SharePointMetadata:
    """Structured metadata for one loaded SharePoint drive item."""

    source_system: str
    site_id: Any
    site_name: Any
    drive_id: Any
    drive_name: Any
    drive_type: Any
    item_id: Any
    item_name: str
    path: str
    size: int
    checksum: str
    etag: Any
    ctag: Any
    created_at: datetime | None
    updated_at: datetime | None
    created_by: str | None
    updated_by: str | None
    parent: SharePointParentReference
    sharepoint_hashes: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata for ``RawDocument.metadata``."""
        return asdict(self)
