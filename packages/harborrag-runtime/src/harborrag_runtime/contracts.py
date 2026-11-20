"""Stable request and response contracts for the runtime SDK.

The ``harborrag`` package re-exports these as public API. Add a field only with
a default, so code that already builds them keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from harborrag_core.contracts.reader import (
    EntityFindRequest as EntityFindRequest,
)
from harborrag_core.contracts.reader import (
    EntityFindResponse as EntityFindResponse,
)
from harborrag_core.contracts.reader import (
    EntityMatch as EntityMatch,
)
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
    """Where the SDK runs an ingestion.

    Attributes:
        DIRECT: Inline in the calling process, without Temporal.
        TEMPORAL: As a durable Temporal workflow that can be paused and cancelled.
    """

    DIRECT = "direct"
    TEMPORAL = "temporal"


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    """One ingestion run of a stored connection, with its scope and tuning.

    Attributes:
        access: Caller and the tenant the run acts in.
        connector_name: ``source_key`` of the connection when ``connection_id`` is unset.
        task_id: Stable run ID. Generated when omitted.
        connection_id: ``source_key`` of the connection. Overrides ``connector_name``.
        source_scope_id: Stable source scope ID. Derived when omitted.
        path: Path inside the connector's source root.
        pattern: Case-insensitive substring, or a glob matched against the path
            relative to the source root.
        recursive: Whether to traverse the source recursively.
        updated_after: Only content updated after this ISO-8601 timestamp.
        limit: Stop after this many artifacts. Positive when set.
        include_attachments: Whether to admit attachments as documents.
        filters: Connector filters. A local connector rejects unknown keys.
        force_reprocess: Whether to reprocess unchanged documents.
        discovery_page_size: Items per discovery page, from 1 to 300.
        discovery_concurrency: Items described at once during discovery, from 1 to 32.
        document_concurrency: Documents processed at once, from 1 to 100.
    """

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
        """Reject blank identities and out-of-range limits.

        Raises:
            ValueError: An identity is blank, or a limit or concurrency is out of range.
        """
        if not self.connector_name.strip() or not self.task_id.strip():
            raise ValueError("ingestion connector and task identities must be non-empty")
        if self.limit is not None and self.limit < 1:
            raise ValueError("ingestion limit must be positive")
        validate_document_concurrency(self.document_concurrency)
        validate_discovery_page_size(self.discovery_page_size)
        validate_discovery_concurrency(self.discovery_concurrency)


@dataclass(frozen=True, slots=True)
class IngestionTaskReference:
    """The durable run that a submit started.

    Attributes:
        task_id: Run ID to pass to status, pause, resume and cancel.
        workflow_id: Temporal workflow ID, when the run has one.
    """

    task_id: str
    workflow_id: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Outcome of a finished ingestion run.

    Attributes:
        task_id: Run ID.
        status: Final run state, such as ``COMPLETED`` or ``PARTIAL``.
        discovered: Documents the run planned to process.
        published: Documents the run published.
        unchanged: Documents left as they were because their content had not changed.
        failed: Documents that failed.
        skipped: Items the run skipped, at discovery or later, for example because a
            document failed to parse. Discovery skips are not in ``discovered``, and
            a later skip also counts in ``failed``.
    """

    task_id: str
    status: str
    discovered: int
    published: int
    unchanged: int
    failed: int
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class IngestionStatus:
    """Workflow state of a durable run, without progress counts.

    Attributes:
        task_id: Run ID.
        status: The workflow's own state, such as ``RUNNING``.
        paused: Whether the run has stopped at a safe boundary after a pause.
        cancel_requested: Whether a cancel was requested.
    """

    task_id: str
    status: str
    paused: bool = False
    cancel_requested: bool = False


@dataclass(frozen=True, slots=True)
class MemoryContextRequest:
    """Identity and question that a conversation-memory operation works on.

    Attributes:
        tenant_id: Tenant the conversation belongs to.
        principal_id: Authenticated subject that owns the session.
        user_id: Identity that user-scoped memory is keyed by.
        session_id: Conversation the turn belongs to.
        question: The turn's question.
        project_id: Project the turn happened in, when the caller supplied one.
    """

    tenant_id: str
    principal_id: str
    user_id: str
    session_id: str
    question: str
    project_id: str | None = None
