"""Transport-independent reader requests and results."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Literal

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, ReadableSource
from harborrag_core.retrieval import (
    GraphNodeResolutionQuery,
    GraphPath,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
)
from harborrag_core.security import AccessContext
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention
from harborrag_core.topology.search import EvidenceBundle, RetrievalMode


class RetrievalLane(StrEnum):
    """Select the retrieval representation used to rank candidates."""

    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"


EVIDENCE_BATCH_LIMIT = 10
DOCUMENT_CONTEXT_LIMIT = 10
SOURCE_LIST_LIMIT = 20
SOURCE_CONNECTOR_FILTER_LIMIT = 10
DOCUMENT_LIST_LIMIT = 20
EVIDENCE_AVAILABILITIES = ("available", "unavailable", "output_limit")
DOCUMENT_CONTEXT_OUTCOMES = ("ok", "unavailable", "version_changed", "output_limit")
type EvidenceAvailability = Literal["available", "unavailable", "output_limit"]
type DocumentContextOutcome = Literal["ok", "unavailable", "version_changed", "output_limit"]


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
        if not 1 <= len(self.items) <= EVIDENCE_BATCH_LIMIT:
            raise ValueError(f"evidence read requires between 1 and {EVIDENCE_BATCH_LIMIT} items")
        chunk_ids = tuple(item.chunk_id for item in self.items)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("evidence read chunk IDs must be unique")


@dataclass(frozen=True, slots=True)
class EvidenceReadItem:
    chunk_id: str
    availability: EvidenceAvailability
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
        if self.offset < 0 or not 1 <= self.limit <= DOCUMENT_CONTEXT_LIMIT:
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
    outcome: DocumentContextOutcome
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
        if not 1 <= self.limit <= SOURCE_LIST_LIMIT:
            raise ValueError(f"source list limit must be between 1 and {SOURCE_LIST_LIMIT}")
        if (
            len(self.source_scope_ids) > SOURCE_LIST_LIMIT
            or len(self.connector_types) > SOURCE_CONNECTOR_FILTER_LIMIT
        ):
            raise ValueError("source list filters exceed their bounds")


@dataclass(frozen=True, slots=True)
class SourceListResponse:
    request_id: str
    sources: tuple[ReadableSource, ...]
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    document_id: str
    document_version_id: str
    title: str | None
    source_scope_id: str
    connector_type: str
    chunk_count: int


@dataclass(frozen=True, slots=True)
class DocumentMetadataRequest:
    access: AccessContext
    document_id: str

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document ID must be non-empty")


@dataclass(frozen=True, slots=True)
class DocumentMetadataResponse:
    request_id: str
    document: DocumentMetadata | None


@dataclass(frozen=True, slots=True)
class DocumentListRequest:
    access: AccessContext
    after_document_id: str | None = None
    limit: int = DOCUMENT_LIST_LIMIT

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= DOCUMENT_LIST_LIMIT:
            raise ValueError(f"document list limit must be between 1 and {DOCUMENT_LIST_LIMIT}")
        if self.after_document_id is not None and not self.after_document_id.strip():
            raise ValueError("document cursor must be non-empty")


@dataclass(frozen=True, slots=True)
class DocumentListResponse:
    request_id: str
    documents: tuple[DocumentMetadata, ...]
    next_document_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphNodeResolveRequest:
    access: AccessContext
    query: GraphNodeResolutionQuery


@dataclass(frozen=True, slots=True)
class GraphNodeResolveResponse:
    request_id: str
    candidates: tuple[GraphNodeRecord, ...]
    truncated: bool = False


EVIDENCE_ITEM_FIELDS = tuple(value.name for value in fields(EvidenceReadItem))
DOCUMENT_CONTEXT_CHUNK_FIELDS = tuple(value.name for value in fields(DocumentContextChunk))


__all__ = [
    "DOCUMENT_LIST_LIMIT",
    "DocumentListRequest",
    "DocumentListResponse",
    "DocumentMetadata",
    "DocumentMetadataRequest",
    "DocumentMetadataResponse",
    "DOCUMENT_CONTEXT_LIMIT",
    "DOCUMENT_CONTEXT_OUTCOMES",
    "DOCUMENT_CONTEXT_CHUNK_FIELDS",
    "EVIDENCE_AVAILABILITIES",
    "EVIDENCE_BATCH_LIMIT",
    "EVIDENCE_ITEM_FIELDS",
    "SOURCE_CONNECTOR_FILTER_LIMIT",
    "SOURCE_LIST_LIMIT",
    "DocumentContextChunk",
    "DocumentContextOutcome",
    "DocumentContextRequest",
    "DocumentContextResponse",
    "EvidenceAvailability",
    "EvidenceReadItem",
    "EvidenceReadRequest",
    "EvidenceReadResponse",
    "EvidenceReadSelector",
    "GraphNodeResolveRequest",
    "GraphNodeResolveResponse",
    "SourceListRequest",
    "SourceListResponse",
    "RetrievalLane",
    "RetrievalMode",
    "RetrievalRequest",
    "RetrievalResponse",
    "EvidenceFetchRequest",
    "EvidenceFetchResponse",
    "EntityResolveRequest",
    "EntityResolveResponse",
    "RelationSearchRequest",
    "RelationSearchResponse",
    "SemanticPathRequest",
    "SemanticPathResponse",
    "GraphTripletRequest",
    "GraphTripletResponse",
    "GraphPathRequest",
    "GraphPathResponse",
    "GraphSubgraphRequest",
    "GraphSubgraphResponse",
]


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    access: AccessContext
    query: str
    top_k: int = 10
    filters: dict[str, object] = field(default_factory=dict)
    lane: RetrievalLane = RetrievalLane.HYBRID
    observe_graph: bool = False
    # Graph nodes to *also* start observation from, beyond the ones the vector
    # results sit on -- the entities a caller already knows are relevant, such
    # as the ones this session's recalled memories reference. Kept last with a
    # default so every existing caller is unaffected, and only read when
    # ``observe_graph`` is on, since it is the graph walk it widens.
    graph_seed_node_keys: tuple[str, ...] = ()
    mode: RetrievalMode = RetrievalMode.FLAT

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", RetrievalMode(self.mode))
        if not self.query.strip():
            raise ValueError("retrieval query must be non-empty")
        if not 1 <= self.top_k <= 100:
            raise ValueError("retrieval top_k must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class RetrievalResponse:
    request_id: str
    lane: RetrievalLane
    results: tuple[RetrievalResult, ...]
    diagnostics: dict[str, object]
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)


@dataclass(frozen=True, slots=True)
class EvidenceFetchRequest:
    access: AccessContext
    chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.chunk_ids) <= 20:
            raise ValueError("evidence fetch requires between 1 and 20 chunk IDs")
        if any(not item.strip() for item in self.chunk_ids):
            raise ValueError("evidence chunk IDs must be non-empty")
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("evidence chunk IDs must be unique")


@dataclass(frozen=True, slots=True)
class EvidenceFetchResponse:
    request_id: str
    results: tuple[RetrievalResult, ...]
    unavailable_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityResolveRequest:
    access: AccessContext
    name: str
    limit: int = 5

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 256:
            raise ValueError("entity name must contain between 1 and 256 characters")
        if not 1 <= self.limit <= 20:
            raise ValueError("entity resolution limit must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class EntityResolveResponse:
    request_id: str
    mentions: tuple[CanonicalMention, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RelationSearchRequest:
    access: AccessContext
    entity_id: str
    predicates: tuple[str, ...] = ()
    direction: str = "either"
    limit: int = 8

    def __post_init__(self) -> None:
        if not self.entity_id.strip():
            raise ValueError("relation search entity ID must be non-empty")
        if self.direction not in {"outgoing", "incoming", "either"}:
            raise ValueError("relation direction must be outgoing, incoming, or either")
        if not 1 <= self.limit <= 30:
            raise ValueError("relation search limit must be between 1 and 30")
        if len(self.predicates) > 5 or any(not item.strip() for item in self.predicates):
            raise ValueError("relation predicates must contain at most five non-empty values")


@dataclass(frozen=True, slots=True)
class RelationSearchResponse:
    request_id: str
    assertions: tuple[CanonicalAssertion, ...]
    mentions: tuple[CanonicalMention, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SemanticPathRequest:
    access: AccessContext
    start_entity_id: str
    end_entity_id: str
    predicates: tuple[str, ...] = ()
    traversal: str = "either"
    max_hops: int = 2
    limit: int = 3

    def __post_init__(self) -> None:
        if not self.start_entity_id.strip() or not self.end_entity_id.strip():
            raise ValueError("semantic path endpoints must be non-empty")
        if self.start_entity_id == self.end_entity_id:
            raise ValueError("semantic path endpoints must be distinct")
        if self.traversal not in {"directed", "either"}:
            raise ValueError("semantic path traversal must be directed or either")
        if not 1 <= self.max_hops <= 3:
            raise ValueError("semantic path max_hops must be between 1 and 3")
        if not 1 <= self.limit <= 5:
            raise ValueError("semantic path limit must be between 1 and 5")
        if len(self.predicates) > 5 or any(not item.strip() for item in self.predicates):
            raise ValueError("path predicates must contain at most five non-empty values")


@dataclass(frozen=True, slots=True)
class SemanticPathResponse:
    request_id: str
    paths: tuple[tuple[CanonicalAssertion, ...], ...]
    mentions: tuple[CanonicalMention, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class GraphTripletRequest:
    access: AccessContext
    query: GraphTripletQuery


@dataclass(frozen=True, slots=True)
class GraphTripletResponse:
    triplets: tuple[GraphTriplet, ...]
    diagnostics: dict[str, object]


@dataclass(frozen=True, slots=True)
class GraphPathRequest:
    access: AccessContext
    query: GraphPathQuery


@dataclass(frozen=True, slots=True)
class GraphPathResponse:
    paths: tuple[GraphPath, ...]
    diagnostics: dict[str, object]


@dataclass(frozen=True, slots=True)
class GraphSubgraphRequest:
    access: AccessContext
    query: GraphSubgraphQuery


@dataclass(frozen=True, slots=True)
class GraphSubgraphResponse:
    nodes: tuple[GraphNodeRecord, ...]
    relations: tuple[GraphEdgeRecord, ...]
    diagnostics: dict[str, object]
