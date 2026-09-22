"""Stable request and response contracts for the runtime SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from harborrag_core.contracts.reader import (
    EntityResolveRequest as EntityResolveRequest,
)
from harborrag_core.contracts.reader import (
    EntityResolveResponse as EntityResolveResponse,
)
from harborrag_core.contracts.reader import (
    EvidenceFetchRequest as EvidenceFetchRequest,
)
from harborrag_core.contracts.reader import (
    EvidenceFetchResponse as EvidenceFetchResponse,
)
from harborrag_core.contracts.reader import (
    GraphPathRequest as GraphPathRequest,
)
from harborrag_core.contracts.reader import (
    GraphPathResponse as GraphPathResponse,
)
from harborrag_core.contracts.reader import (
    GraphSubgraphRequest as GraphSubgraphRequest,
)
from harborrag_core.contracts.reader import (
    GraphSubgraphResponse as GraphSubgraphResponse,
)
from harborrag_core.contracts.reader import (
    GraphTripletRequest as GraphTripletRequest,
)
from harborrag_core.contracts.reader import (
    GraphTripletResponse as GraphTripletResponse,
)
from harborrag_core.contracts.reader import (
    RelationSearchRequest as RelationSearchRequest,
)
from harborrag_core.contracts.reader import (
    RelationSearchResponse as RelationSearchResponse,
)
from harborrag_core.contracts.reader import (
    RetrievalLane as RetrievalLane,
)
from harborrag_core.contracts.reader import (
    RetrievalMode as RetrievalMode,
)
from harborrag_core.contracts.reader import (
    RetrievalRequest as RetrievalRequest,
)
from harborrag_core.contracts.reader import (
    RetrievalResponse as RetrievalResponse,
)
from harborrag_core.contracts.reader import (
    SemanticPathRequest as SemanticPathRequest,
)
from harborrag_core.contracts.reader import (
    SemanticPathResponse as SemanticPathResponse,
)
from harborrag_core.security import AccessContext
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
class MemoryContextRequest:
    tenant_id: str
    principal_id: str
    user_id: str
    session_id: str
    question: str
    project_id: str | None = None
