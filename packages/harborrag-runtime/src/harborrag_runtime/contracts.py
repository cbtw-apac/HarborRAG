"""Stable request and response contracts for the runtime SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord
from harborrag_core.retrieval import (
    GraphPath,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
)
from harborrag_core.security import AccessContext
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention
from harborrag_core.topology.search import EvidenceBundle, RetrievalMode
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.ingestion.limits import (
    validate_discovery_concurrency,
    validate_discovery_page_size,
    validate_document_concurrency,
)
from harborrag_runtime.reader_contracts import (
    DocumentContextChunk as DocumentContextChunk,
)
from harborrag_runtime.reader_contracts import (
    DocumentContextRequest as DocumentContextRequest,
)
from harborrag_runtime.reader_contracts import (
    DocumentContextResponse as DocumentContextResponse,
)
from harborrag_runtime.reader_contracts import (
    EvidenceReadItem as EvidenceReadItem,
)
from harborrag_runtime.reader_contracts import (
    EvidenceReadRequest as EvidenceReadRequest,
)
from harborrag_runtime.reader_contracts import (
    EvidenceReadResponse as EvidenceReadResponse,
)
from harborrag_runtime.reader_contracts import (
    EvidenceReadSelector as EvidenceReadSelector,
)
from harborrag_runtime.reader_contracts import (
    GraphNodeResolveRequest as GraphNodeResolveRequest,
)
from harborrag_runtime.reader_contracts import (
    GraphNodeResolveResponse as GraphNodeResolveResponse,
)
from harborrag_runtime.reader_contracts import (
    SourceListRequest as SourceListRequest,
)
from harborrag_runtime.reader_contracts import (
    SourceListResponse as SourceListResponse,
)


class ExecutionMode(StrEnum):
    DIRECT = "direct"
    TEMPORAL = "temporal"


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    access: AccessContext
    connector_name: str
    task_id: str = field(default_factory=lambda: f"ingest-{uuid4().hex}")
    connection_id: str | None = None
    source_scope_id: str | None = None
    path: str | None = None
    pattern: str | None = None
    recursive: bool = True
    updated_after: str | None = None
    limit: int | None = None
    include_attachments: bool = True
    filters: dict[str, object] = field(default_factory=dict)
    force_reprocess: bool = False
    discovery_page_size: int = 50
    discovery_concurrency: int = 4
    document_concurrency: int = 8

    def __post_init__(self) -> None:
        if not self.connector_name.strip() or not self.task_id.strip():
            raise ValueError("ingestion connector and task identities must be non-empty")
        if self.limit is not None and self.limit < 1:
            raise ValueError("ingestion limit must be positive")
        validate_document_concurrency(self.document_concurrency)
        validate_discovery_page_size(self.discovery_page_size)
        validate_discovery_concurrency(self.discovery_concurrency)


@dataclass(frozen=True, slots=True)
class IngestionTaskReference:
    task_id: str
    workflow_id: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    task_id: str
    status: str
    discovered: int
    published: int
    unchanged: int
    failed: int


@dataclass(frozen=True, slots=True)
class IngestionStatus:
    task_id: str
    status: str
    paused: bool = False
    cancel_requested: bool = False


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


@dataclass(frozen=True, slots=True)
class MemoryContextRequest:
    tenant_id: str
    principal_id: str
    user_id: str
    session_id: str
    question: str
    project_id: str | None = None
