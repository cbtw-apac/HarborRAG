"""Temporal workflow and activity payloads for ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.ingestion.limits import MAX_RETRY_DOCUMENT_IDS
from harborrag_core.ingestion.run_contracts import PreparedSourceSubmission
from harborrag_runtime.temporal_models import TemporalWorkflowOptions

from .dispatch import DocumentDispatchSummary

_SOURCE_TASK_STATES = frozenset(
    "PENDING RUNNING PAUSED CANCELLING COMPLETED PARTIAL FAILED CANCELLED".split()
)


@dataclass(frozen=True, slots=True)
class WorkflowArtifactReference:
    """Reference to a workflow artifact in the object store."""

    bucket: str
    key: str
    sha256: str
    byte_size: int
    media_type: str
    byte_offset: int | None = None
    byte_length: int | None = None

    def __post_init__(self) -> None:
        if not self.bucket.strip() or not self.key.strip():
            raise ValueError("artifact bucket and key must be non-empty")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("artifact sha256 must be lowercase hexadecimal")
        if not self.media_type.strip():
            raise ValueError("artifact media_type must be non-empty")
        if self.byte_size < 0:
            raise ValueError("artifact byte_size must not be negative")
        if (self.byte_offset is None) != (self.byte_length is None):
            raise ValueError("artifact range values must be set together")
        if self.byte_offset is not None:
            assert self.byte_length is not None
            if self.byte_offset < 0 or self.byte_length < 0:
                raise ValueError("artifact range values must not be negative")
            if self.byte_offset + self.byte_length > self.byte_size:
                raise ValueError("artifact range exceeds the artifact size")


@dataclass(frozen=True, slots=True)
class SourceContinuation:
    """Small durable cursor carried across bounded workflow histories."""

    scan_id: str
    plan_reference: WorkflowArtifactReference
    document_count: int
    next_document_index: int
    batch_number: int
    summary: DocumentDispatchSummary
    skipped_by_reason: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.scan_id.strip():
            raise ValueError("continued source scan ID must be non-empty")
        if not 0 <= self.next_document_index <= self.document_count:
            raise ValueError("continued source document cursor is invalid")
        if self.document_count < 0 or self.batch_number < 0:
            raise ValueError("continued source counters must not be negative")


@dataclass(frozen=True, slots=True)
class SourceIngestionInput(PreparedSourceSubmission):
    """Temporal history payload extends the neutral submission with replay state."""

    continuation: SourceContinuation | None = None
    workflow_options: TemporalWorkflowOptions = field(default_factory=TemporalWorkflowOptions)


@dataclass(frozen=True, slots=True)
class ScheduledSourceIngestionInput:
    """Schedule action payload: only the target, prepared afresh on each firing.

    A prepared source frozen at schedule creation would carry a stale connector
    fingerprint, processing profile and task queues once the worker config moves.
    """

    schedule_id: str
    tenant_id: str
    connection_id: str
    source_scope_id: str | None = None
    force_reprocess: bool = False

    def __post_init__(self) -> None:
        if not self.schedule_id.strip():
            raise ValueError("schedule_id must be non-empty")


@dataclass(frozen=True, slots=True)
class ScheduledSourcePreparationInput:
    """Input to prepare a scheduled source run."""

    task_id: str
    tenant_id: str
    connection_id: str
    source_scope_id: str | None = None
    force_reprocess: bool = False


@dataclass(frozen=True, slots=True)
class ScheduledSourcePreparation:
    """A source prepared by the worker; ``source`` is None while its scope is busy."""

    source: SourceIngestionInput | None
    source_scope_id: str


@dataclass(frozen=True, slots=True)
class ScheduledSourceIngestionResult:
    """Result of a scheduled source ingestion."""

    schedule_id: str
    outcome: str
    task_id: str | None = None
    result: SourceIngestionResult | None = None


@dataclass(frozen=True, slots=True)
class ScheduledRunMetricInput:
    """Metrics of one scheduled run."""

    schedule_id: str
    outcome: str
    schedule_to_start_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SourceDiscoveryResult:
    """Result of discovering a source's items."""

    scan_id: str
    plan_reference: WorkflowArtifactReference
    document_count: int
    skipped_by_reason: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceBatchInput:
    """Input to process one batch of a source's documents."""

    task_id: str
    tenant_id: str
    connector_name: str
    plan_reference: WorkflowArtifactReference
    start_index: int
    end_index: int
    batch_number: int
    document_concurrency: int
    workflow_options: TemporalWorkflowOptions = field(default_factory=TemporalWorkflowOptions)


@dataclass(frozen=True, slots=True)
class DocumentIngestionInput:
    """Input to ingest one document."""

    task_id: str
    tenant_id: str
    connector_name: str
    plan_reference: WorkflowArtifactReference
    document_index: int
    workflow_options: TemporalWorkflowOptions = field(default_factory=TemporalWorkflowOptions)


@dataclass(frozen=True, slots=True)
class RawCaptureResult:
    """Result of capturing a document's raw bytes."""

    document: DocumentIngestionInput
    document_id: str
    document_version_id: str | None
    decision: str
    connector_type: str | None = None
    content_hash: str | None = None
    source_artifact: WorkflowArtifactReference | None = None
    metadata_artifact: WorkflowArtifactReference | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip() or not self.decision.strip():
            raise ValueError("raw capture result identity must be non-empty")
        raw_values = (
            self.connector_type,
            self.content_hash,
            self.source_artifact,
            self.metadata_artifact,
        )
        has_raw = all(value is not None for value in raw_values)
        if any(value is not None for value in raw_values) and not has_raw:
            raise ValueError("raw capture reference must be complete")
        if not has_raw and self.document_version_id is None:
            raise ValueError("raw capture without bytes requires an active document version")


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    """A document after parsing and normalization."""

    document: DocumentIngestionInput
    document_id: str
    document_version_id: str
    decision: str
    canonical_reference: WorkflowArtifactReference | None = None

    def __post_init__(self) -> None:
        identities = (self.document_id, self.document_version_id, self.decision)
        if any(not value.strip() for value in identities):
            raise ValueError("prepared document identity must be non-empty")


@dataclass(frozen=True, slots=True)
class DocumentFailureInput:
    """Input to record a failed document."""

    document: DocumentIngestionInput
    prepared: PreparedDocument | None
    failed_stage: str
    error_type: str
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.failed_stage.strip() or not self.error_type.strip():
            raise ValueError("document failure stage and type must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceFinalizationInput:
    """Input to finalize a source ingestion."""

    source: SourceIngestionInput
    scan_id: str
    plan_reference: WorkflowArtifactReference
    summary: DocumentDispatchSummary


@dataclass(frozen=True, slots=True)
class SourceCancellationInput:
    """Input to cancel a source ingestion."""

    task_id: str

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("cancelled source task ID must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceCancellationResult:
    """Skips of a source ingestion when it was cancelled.

    Attributes:
        skipped_by_reason: Items skipped at discovery or per document, by reason code.
    """

    skipped_by_reason: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourcePauseInput:
    """Input to pause a source ingestion."""

    task_id: str

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("paused source task ID must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceResumeInput:
    """Input to resume a source ingestion."""

    task_id: str

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("resumed source task ID must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceFailureInput:
    """Input to record a source ingestion failure."""

    task_id: str
    error_code: str

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.error_code.strip():
            raise ValueError("source failure task ID and error code must be non-empty")


MAX_REPORTED_REMOVAL_CANDIDATES = 1_000
"""A result travels through Temporal as one payload, capped at 2 MB. Each removed document id is
about 75 bytes, so a rescan that retires tens of thousands of documents cannot carry them all;
the retirements are durable in the control plane anyway, so the result names a bounded sample and
the full count.
"""


def reported_removal_candidates(removals: tuple[str, ...]) -> tuple[str, ...]:
    """The removed document ids a Temporal result may carry."""
    return removals[:MAX_REPORTED_REMOVAL_CANDIDATES]


@dataclass(frozen=True, slots=True)
class SourceIngestionResult:
    """Result of a source ingestion."""

    task_id: str
    scan_id: str
    discovered: int
    published: int
    unchanged: int
    failed: int
    removal_candidates: tuple[str, ...]
    """At most MAX_REPORTED_REMOVAL_CANDIDATES of the removed documents."""
    unresolved_relations: int
    status: str = "COMPLETED"
    removal_count: int | None = None
    """Every removed document, including those not named above. Results recorded before this field
    existed carry no value and are read as the named ones.
    """
    skipped: int = 0
    """Items the run skipped at discovery or in its document results; older results read 0."""
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    """``skipped`` counted by reason code; older results read no reasons."""

    def __post_init__(self) -> None:
        if self.status not in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
            raise ValueError("source result has an unsupported status")
        if len(self.removal_candidates) > MAX_REPORTED_REMOVAL_CANDIDATES:
            raise ValueError("source result names more removals than a payload may carry")
        if self.removal_count is None:
            object.__setattr__(self, "removal_count", len(self.removal_candidates))
        elif self.removal_count < len(self.removal_candidates):
            raise ValueError("source result removal count is below the removals it names")


@dataclass(frozen=True, slots=True)
class SourceIngestionStatus:
    """Status of a source ingestion workflow."""

    task_id: str
    status: str
    paused: bool
    cancel_requested: bool
    pause_applied: bool = False
    native_pause_pending: bool = False

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("source status task ID must be non-empty")
        if self.status not in _SOURCE_TASK_STATES:
            raise ValueError("source status is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowExecutionControlInput:
    """Input to pause or unpause a workflow execution."""

    workflow_id: str
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.workflow_id.strip():
            raise ValueError("workflow control workflow ID must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceBatchStatus:
    """Status of one source batch."""

    task_id: str
    status: str
    paused: bool
    cancel_requested: bool

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("source batch status task ID must be non-empty")
        if self.status not in {"PENDING", "RUNNING", "PAUSED", "CANCELLING", "COMPLETED"}:
            raise ValueError("source batch status is invalid")


@dataclass(frozen=True, slots=True)
class RetryFailuresInput:
    """Input of a failure retry workflow."""

    retry_task_id: str
    original_task_id: str
    tenant_id: str
    document_ids: tuple[str, ...]
    document_concurrency: int = 8
    workflow_options: TemporalWorkflowOptions = field(default_factory=TemporalWorkflowOptions)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.retry_task_id, self.original_task_id, self.tenant_id)
        ):
            raise ValueError("retry task identities must be non-empty")
        if not self.document_ids or any(not value.strip() for value in self.document_ids):
            raise ValueError("retry task document IDs must be non-empty")
        if len(self.document_ids) > MAX_RETRY_DOCUMENT_IDS:
            raise ValueError(
                f"retry task document IDs must number at most {MAX_RETRY_DOCUMENT_IDS} "
                "per retry run (RetryFailuresWorkflow has no continue_as_new checkpoint)"
            )
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("retry task document IDs must be unique")
        if not 1 <= self.document_concurrency <= 100:
            raise ValueError("retry document_concurrency must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class RetryPreparationResult:
    """Result of preparing a failure retry."""

    plan_reference: WorkflowArtifactReference
    document_count: int


@dataclass(frozen=True, slots=True)
class RetryDocumentInput:
    """Input to retry one document."""

    retry_task_id: str
    original_task_id: str
    tenant_id: str
    plan_reference: WorkflowArtifactReference
    document_index: int
    workflow_options: TemporalWorkflowOptions = field(default_factory=TemporalWorkflowOptions)


@dataclass(frozen=True, slots=True)
class RetryDocumentFailureInput:
    """Input to record a document that failed during a retry."""

    document: RetryDocumentInput
    error_type: str
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RetryTaskFailureInput:
    """Input to record that a retry task failed."""

    retry_task_id: str
    error_code: str

    def __post_init__(self) -> None:
        if not self.retry_task_id.strip() or not self.error_code.strip():
            raise ValueError("retry task failure ID and error code must be non-empty")


@dataclass(frozen=True, slots=True)
class RetryFinalizationInput:
    """Input to finalize a failure retry."""

    retry_task_id: str
    selected: int
    summary: DocumentDispatchSummary


@dataclass(frozen=True, slots=True)
class RetryFailuresResult:
    """Result of a failure retry workflow."""

    retry_task_id: str
    selected: int
    published: int
    unchanged: int
    failed: int
    status: str
