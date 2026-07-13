"""Structured schemas used by the Confluence connector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from harborrag_adapters.connectors.shared.attachments import (
    AttachmentMetadata,
)


@dataclass(slots=True)
class ConfluencePageReference:
    """Compact page reference used for hierarchy metadata."""

    id: Any
    title: Any
    type: str


@dataclass(slots=True)
class ConfluenceCommentMetadata:
    """Normalized Confluence comment metadata."""

    id: Any
    body: str
    author: Any
    created_at: Any


@dataclass(slots=True)
class ConfluenceHierarchyMetadata:
    """Flat hierarchy fields exposed on Confluence document metadata."""

    ancestors: list[ConfluencePageReference]
    children: list[ConfluencePageReference]
    depth: int
    breadcrumb: list[str]


@dataclass(slots=True)
class ConfluenceMetadata:
    """Structured metadata for one loaded Confluence content item."""

    source_system: str
    content_id: str
    content_type: str
    title: str
    space_key: str
    version: Any
    author: str | None
    created_at: datetime | None
    updated_at: datetime | None
    labels: list[str]
    checksum: str
    comments: list[ConfluenceCommentMetadata]
    attachments: list[AttachmentMetadata]
    ancestors: list[ConfluencePageReference]
    children: list[ConfluencePageReference]
    depth: int
    breadcrumb: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata for ``RawDocument.metadata``."""
        payload = asdict(self)
        for key in ("created_at", "updated_at"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload
