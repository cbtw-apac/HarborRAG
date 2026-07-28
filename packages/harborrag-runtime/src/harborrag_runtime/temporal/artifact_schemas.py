"""Artifact-level schemas shared by Temporal activities and workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from harborrag_runtime.temporal.schema_version import PAYLOAD_VERSION


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
