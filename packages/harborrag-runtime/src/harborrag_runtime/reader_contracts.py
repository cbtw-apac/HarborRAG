"""Bounded evidence, context, source, and graph-resolution SDK contracts."""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.ingestion import GraphNodeRecord, ReadableSource
from harborrag_core.retrieval import GraphNodeResolutionQuery
from harborrag_core.security import AccessContext


@dataclass(frozen=True, slots=True)
class EvidenceReadSelector:
    chunk_id: str
    expected_document_id: str | None = None
    expected_document_version_id: str | None = None

    def __post_init__(self) -> None:
        if not self.chunk_id.strip():
            raise ValueError("evidence chunk ID must be non-empty")
        if self.expected_document_id is not None and not self.expected_document_id.strip():
            raise ValueError("expected document ID must be non-empty when provided")
        if (
            self.expected_document_version_id is not None
            and not self.expected_document_version_id.strip()
        ):
            raise ValueError("expected document version ID must be non-empty when provided")


@dataclass(frozen=True, slots=True)
class EvidenceReadRequest:
    access: AccessContext
    items: tuple[EvidenceReadSelector, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.items) <= 10:
            raise ValueError("evidence read requires between 1 and 10 items")
        chunk_ids = tuple(item.chunk_id for item in self.items)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("evidence read chunk IDs must be unique")


@dataclass(frozen=True, slots=True)
class EvidenceReadItem:
    chunk_id: str
    availability: str
    text: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    document_title: str | None = None
    source_scope_id: str | None = None
    connector_type: str | None = None
    chunk_kind: str | None = None
    ordinal: int | None = None
    section_path: tuple[str, ...] = ()
    citation_locator: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceReadResponse:
    request_id: str
    items: tuple[EvidenceReadItem, ...]


@dataclass(frozen=True, slots=True)
class DocumentContextRequest:
    access: AccessContext
    document_id: str
    expected_document_version_id: str | None = None
    anchor_chunk_id: str | None = None
    anchor_section_path: tuple[str, ...] = ()
    offset: int = 0
    limit: int = 10
    include_outline: bool = False

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("context document ID must be non-empty")
        if self.expected_document_version_id is not None and not (
            self.expected_document_version_id.strip()
        ):
            raise ValueError("expected document version ID must be non-empty when provided")
        if self.anchor_chunk_id is not None and not self.anchor_chunk_id.strip():
            raise ValueError("anchor chunk ID must be non-empty when provided")
        if any(not part.strip() for part in self.anchor_section_path):
            raise ValueError("anchor section path entries must be non-empty")
        if self.anchor_chunk_id is not None and self.anchor_section_path:
            raise ValueError("use either anchor_chunk_id or anchor_section_path")
        if self.offset < 0 or not 1 <= self.limit <= 10:
            raise ValueError("context offset or limit is outside its bounded range")


@dataclass(frozen=True, slots=True)
class DocumentContextChunk:
    chunk_id: str
    ordinal: int
    text: str
    chunk_kind: str
    section_path: tuple[str, ...]
    citation_locator: dict[str, object]


@dataclass(frozen=True, slots=True)
class DocumentContextResponse:
    request_id: str
    outcome: str
    document_id: str
    document_version_id: str | None = None
    chunks: tuple[DocumentContextChunk, ...] = ()
    outline: tuple[tuple[str, ...], ...] = ()
    next_offset: int | None = None


@dataclass(frozen=True, slots=True)
class SourceListRequest:
    access: AccessContext
    source_scope_ids: tuple[str, ...] = ()
    connector_types: tuple[str, ...] = ()
    after_source_scope_id: str | None = None
    limit: int = 20

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 20:
            raise ValueError("source list limit must be between 1 and 20")
        if len(self.source_scope_ids) > 20 or len(self.connector_types) > 10:
            raise ValueError("source list filters exceed their bounds")


@dataclass(frozen=True, slots=True)
class SourceListResponse:
    request_id: str
    sources: tuple[ReadableSource, ...]
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class GraphNodeResolveRequest:
    access: AccessContext
    query: GraphNodeResolutionQuery


@dataclass(frozen=True, slots=True)
class GraphNodeResolveResponse:
    request_id: str
    candidates: tuple[GraphNodeRecord, ...]
    truncated: bool = False


__all__ = [
    "DocumentContextChunk",
    "DocumentContextRequest",
    "DocumentContextResponse",
    "EvidenceReadItem",
    "EvidenceReadRequest",
    "EvidenceReadResponse",
    "EvidenceReadSelector",
    "GraphNodeResolveRequest",
    "GraphNodeResolveResponse",
    "SourceListRequest",
    "SourceListResponse",
]
