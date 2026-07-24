"""Versioned, compact schemas stored in Temporal workflow history."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from harborrag_runtime.temporal.retry import ActivityRetryConfig
from harborrag_runtime.temporal.task_queues import TaskQueueConfig

PAYLOAD_VERSION = 1


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class ArtifactStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_RESOLUTION = "waiting_for_resolution"
    SUCCEEDED = "succeeded"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    SKIPPED = "skipped"
    QUARANTINED = "quarantined"
    CANCELLED = "cancelled"


class ArtifactStage(StrEnum):
    PREFLIGHT = "preflight"
    FETCH = "fetch"
    PARSE = "parse"
    CHUNK = "chunk"
    INDEX = "index"
    VALIDATE = "validate"
    FINALIZE = "finalize"


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    source_ref: str
    source_kind: str
    connector_name: str
    artifact_revision_id: str | None = None
    checksum: str | None = None
    parser_hint: str | None = None
    requires_ocr: bool = False
    version: int = PAYLOAD_VERSION

    def __post_init__(self) -> None:
        values = (self.artifact_id, self.source_ref, self.source_kind, self.connector_name)
        if self.version != PAYLOAD_VERSION or any(not value.strip() for value in values):
            raise ValueError("artifact reference is invalid or unsupported")


@dataclass(frozen=True, slots=True)
class ArtifactStageState:
    artifact: ArtifactReference
    generation_id: str
    stage: ArtifactStage = ArtifactStage.PREFLIGHT
    artifact_revision_id: str | None = None
    snapshot_ref: str | None = None
    parsed_document_ref: str | None = None
    chunking_result_ref: str | None = None
    indexing_result_ref: str | None = None
    checkpoint_ref: str | None = None
    version: int = PAYLOAD_VERSION

    def __post_init__(self) -> None:
        if self.version != PAYLOAD_VERSION or not self.generation_id.strip():
            raise ValueError("artifact stage state is invalid or unsupported")


@dataclass(frozen=True, slots=True)
class PendingResolution:
    artifact_id: str
    request_ref: str
    reason: str
    resume_stage: ArtifactStage
    workflow_id: str | None = None
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    artifact_id: str
    request_ref: str
    decision: str
    actor_id: str
    submitted_at: str | None = None
    note: str | None = None
    version: int = PAYLOAD_VERSION

    def __post_init__(self) -> None:
        values = (self.artifact_id, self.request_ref, self.decision, self.actor_id)
        if self.version != PAYLOAD_VERSION or any(not value.strip() for value in values):
            raise ValueError("resolution decision is invalid or unsupported")


@dataclass(frozen=True, slots=True)
class ResolutionReceipt:
    artifact_id: str
    decision_ref: str
    accepted: bool
    resume_stage: ArtifactStage
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ArtifactActivityInput:
    run_id: str
    tenant_id: str
    manifest_id: str
    state: ArtifactStageState
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ArtifactActivityResult:
    status: ArtifactStatus
    state: ArtifactStageState
    pending_resolution: PendingResolution | None = None
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool = False
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class DiscoveryInput:
    run_id: str
    tenant_id: str
    manifest_id: str
    connector_name: str
    cursor: str | None
    page_size: int
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    artifacts: tuple[ArtifactReference, ...]
    next_cursor: str | None
    checkpoint_ref: str
    done: bool
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class HeartbeatProgress:
    stage: str
    completed: int
    total: int | None = None
    cursor: str | None = None
    checkpoint_ref: str | None = None
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ArtifactWorkflowInput:
    run_id: str
    tenant_id: str
    manifest_id: str
    artifact: ArtifactReference
    generation_id: str
    options: WorkflowOptions
    attempt: int = 0
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    artifact_id: str
    status: ArtifactStatus
    artifact_revision_id: str | None = None
    generation_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    pending_resolution: PendingResolution | None = None
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class RunProgress:
    discovered: int = 0
    processed: int = 0
    succeeded: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped: int = 0
    quarantined: int = 0
    cancelled: int = 0
    partitions: int = 0

    def add_artifact(self, status: ArtifactStatus) -> RunProgress:
        field = {
            ArtifactStatus.SUCCEEDED: "succeeded",
            ArtifactStatus.UNCHANGED: "unchanged",
            ArtifactStatus.FAILED: "failed",
            ArtifactStatus.SKIPPED: "skipped",
            ArtifactStatus.QUARANTINED: "quarantined",
            ArtifactStatus.CANCELLED: "cancelled",
        }.get(status)
        values = {"processed": self.processed + 1}
        if field is not None:
            values[field] = getattr(self, field) + 1
        return replace(self, **values)

    def merge(self, other: RunProgress) -> RunProgress:
        return RunProgress(
            discovered=self.discovered + other.discovered,
            processed=self.processed + other.processed,
            succeeded=self.succeeded + other.succeeded,
            unchanged=self.unchanged + other.unchanged,
            failed=self.failed + other.failed,
            skipped=self.skipped + other.skipped,
            quarantined=self.quarantined + other.quarantined,
            cancelled=self.cancelled + other.cancelled,
            partitions=self.partitions + other.partitions,
        )


@dataclass(frozen=True, slots=True)
class PartitionInput:
    run_id: str
    tenant_id: str
    manifest_id: str
    partition_number: int
    artifacts: tuple[ArtifactReference, ...]
    generation_id: str
    options: WorkflowOptions
    attempt: int = 0
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class PartitionResult:
    partition_number: int
    progress: RunProgress
    failed_artifacts: tuple[str, ...] = ()
    quarantined_artifacts: tuple[str, ...] = ()
    pending_resolutions: tuple[PendingResolution, ...] = ()
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class WorkflowOptions:
    """Workflow-stable snapshot of orchestration configuration."""

    task_queues: TaskQueueConfig = TaskQueueConfig()
    retries: ActivityRetryConfig = ActivityRetryConfig()
    partition_size: int = 50
    partition_concurrency: int = 4
    artifact_concurrency: int = 16
    continue_after_partitions: int = 100
    continue_after_artifacts: int = 2_000
    version: int = PAYLOAD_VERSION

    def __post_init__(self) -> None:
        values = (
            self.partition_size,
            self.partition_concurrency,
            self.artifact_concurrency,
            self.continue_after_partitions,
            self.continue_after_artifacts,
        )
        if self.version != PAYLOAD_VERSION or any(value < 1 for value in values):
            raise ValueError("workflow orchestration limits must be positive")


@dataclass(frozen=True, slots=True)
class IngestionRunInput:
    run_id: str
    tenant_id: str
    connector_name: str
    manifest_id: str
    generation_id: str
    options: WorkflowOptions = WorkflowOptions()
    source_cursor: str | None = None
    next_partition: int = 0
    progress: RunProgress = RunProgress()
    paused: bool = False
    version: int = PAYLOAD_VERSION

    def __post_init__(self) -> None:
        values = (
            self.run_id,
            self.tenant_id,
            self.connector_name,
            self.manifest_id,
            self.generation_id,
        )
        if self.version != PAYLOAD_VERSION or any(not value.strip() for value in values):
            raise ValueError("ingestion run input is invalid or unsupported")


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    run_id: str
    manifest_id: str
    status: RunStatus
    progress: RunProgress
    reconciliation_ref: str | None = None
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class WorkflowStatusView:
    run_id: str
    status: RunStatus
    progress: RunProgress
    current_partition: int | None
    paused: bool
    cancel_requested: bool
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ConcurrencyUpdate:
    partition_concurrency: int
    artifact_concurrency: int
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ReconciliationInput:
    run_id: str
    tenant_id: str
    manifest_id: str
    generation_id: str
    progress: RunProgress
    cancelled: bool = False
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    reconciliation_ref: str
    status: RunStatus
    version: int = PAYLOAD_VERSION
