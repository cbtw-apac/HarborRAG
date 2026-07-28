"""Versioned, compact schemas stored in Temporal workflow history."""

from __future__ import annotations

from dataclasses import dataclass, replace

from harborrag_runtime.temporal.artifact_schemas import (
    ArtifactActivityInput as ArtifactActivityInput,
)
from harborrag_runtime.temporal.artifact_schemas import (
    ArtifactActivityResult as ArtifactActivityResult,
)
from harborrag_runtime.temporal.artifact_schemas import (
    ArtifactReference,
    ArtifactStatus,
    PendingResolution,
    RunStatus,
)
from harborrag_runtime.temporal.artifact_schemas import (
    ArtifactStage as ArtifactStage,
)
from harborrag_runtime.temporal.artifact_schemas import (
    ArtifactStageState as ArtifactStageState,
)
from harborrag_runtime.temporal.artifact_schemas import (
    ResolutionDecision as ResolutionDecision,
)
from harborrag_runtime.temporal.artifact_schemas import (
    ResolutionReceipt as ResolutionReceipt,
)
from harborrag_runtime.temporal.retry import ActivityRetryConfig
from harborrag_runtime.temporal.schema_version import PAYLOAD_VERSION
from harborrag_runtime.temporal.task_queues import TaskQueueConfig


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
class ArtifactOutcomeCheckpoint:
    """Compact reference and current retryable terminal outcome for one artifact."""

    reference: ArtifactReference
    result: ArtifactResult
    version: int = PAYLOAD_VERSION

    def __post_init__(self) -> None:
        if (
            self.version != PAYLOAD_VERSION
            or self.reference.artifact_id != self.result.artifact_id
            or self.result.status not in {ArtifactStatus.FAILED, ArtifactStatus.QUARANTINED}
        ):
            raise ValueError("artifact outcome checkpoint is invalid or unsupported")


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

    def replace_artifact(
        self,
        previous: ArtifactStatus,
        current: ArtifactStatus,
    ) -> RunProgress:
        """Replace a prior terminal outcome without counting another artifact."""

        fields = {
            ArtifactStatus.SUCCEEDED: "succeeded",
            ArtifactStatus.UNCHANGED: "unchanged",
            ArtifactStatus.FAILED: "failed",
            ArtifactStatus.SKIPPED: "skipped",
            ArtifactStatus.QUARANTINED: "quarantined",
            ArtifactStatus.CANCELLED: "cancelled",
        }
        previous_field = fields.get(previous)
        current_field = fields.get(current)
        values: dict[str, int] = {}
        if previous_field is not None:
            values[previous_field] = getattr(self, previous_field) - 1
        if current_field is not None:
            values[current_field] = (
                values.get(
                    current_field,
                    getattr(self, current_field),
                )
                + 1
            )
        return replace(self, **values)


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
    artifact_results: tuple[ArtifactResult, ...] = ()
    version: int = PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class RunContinuationState:
    """Operator-visible state that must survive continue-as-new."""

    artifact_outcomes: tuple[ArtifactOutcomeCheckpoint, ...] = ()
    pending_resolutions: tuple[PendingResolution, ...] = ()
    retry_requested: tuple[str, ...] = ()
    partition_concurrency: int | None = None
    artifact_concurrency: int | None = None
    retry_attempt: int = 0
    cancel_requested: bool = False
    version: int = PAYLOAD_VERSION

    def __post_init__(self) -> None:
        capacities = (self.partition_concurrency, self.artifact_concurrency)
        if (
            self.version != PAYLOAD_VERSION
            or self.retry_attempt < 0
            or any(value is not None and value < 1 for value in capacities)
        ):
            raise ValueError("run continuation state is invalid or unsupported")
        artifact_ids = [checkpoint.reference.artifact_id for checkpoint in self.artifact_outcomes]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("run continuation outcomes must have unique artifact IDs")


@dataclass(frozen=True, slots=True)
class WorkflowOptions:
    """Workflow-stable snapshot of orchestration configuration."""

    task_queues: TaskQueueConfig = TaskQueueConfig()
    retries: ActivityRetryConfig = ActivityRetryConfig()
    max_artifacts: int | None = None
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
        if (
            self.version != PAYLOAD_VERSION
            or any(value < 1 for value in values)
            or (self.max_artifacts is not None and self.max_artifacts < 1)
        ):
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
    continuation: RunContinuationState = RunContinuationState()
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
