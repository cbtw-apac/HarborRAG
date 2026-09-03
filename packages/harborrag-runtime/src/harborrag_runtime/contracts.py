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
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.ingestion.limits import (
    validate_discovery_concurrency,
    validate_discovery_page_size,
    validate_document_concurrency,
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

    def __post_init__(self) -> None:
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


@dataclass(frozen=True, slots=True)
class ExpandDocumentRequest:
    access: AccessContext
    document_id: str

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("expand_document requires a non-empty document_id")


@dataclass(frozen=True, slots=True)
class ExpandDocumentRelation:
    predicate: str
    target_id: str
    target_type: str


@dataclass(frozen=True, slots=True)
class ExpandDocumentResponse:
    document_id: str
    document_version_id: str
    title: str
    content_type: str
    text: str
    relations: tuple[ExpandDocumentRelation, ...] = ()


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
