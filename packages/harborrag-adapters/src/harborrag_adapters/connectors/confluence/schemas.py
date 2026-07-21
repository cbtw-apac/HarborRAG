"""Structured schemas used by the Confluence connector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from harborrag_adapters.connectors.schemas import ConnectorMetadata
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


@dataclass(slots=True, kw_only=True)
class ConfluenceMetadata(ConnectorMetadata):
    """Structured metadata for one loaded Confluence content item."""

    source_system: ClassVar[str] = "confluence"

    content_id: str
    content_type: str
    space_key: str
    version: Any
    author: str | None
    labels: list[str]
    comments: list[ConfluenceCommentMetadata]
    attachments: list[AttachmentMetadata]
    ancestors: list[ConfluencePageReference]
    children: list[ConfluencePageReference]
    depth: int
    breadcrumb: list[str]
