"""Validated request and response schemas for the Control Plane API."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)


class HarborAPISchema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskOperation(StrEnum):
    INGEST = "ingest"
    RETRIEVE = "retrieve"


class TaskError(HarborAPISchema):
    code: str = Field(
        min_length=1,
        max_length=100,
        description="Stable machine-readable error code.",
    )
    message: str = Field(
        min_length=1,
        max_length=2_000,
        description="Human-readable error explanation.",
    )
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class TaskProgress(HarborAPISchema):
    stage: str = Field(
        min_length=1,
        max_length=100,
        description="Current logical processing stage.",
    )
    completed: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        if self.total is not None and self.completed > self.total:
            raise ValueError("completed cannot be greater than total")

        return self


class IngestSourceInput(HarborAPISchema):
    connector: str = Field(
        min_length=1,
        max_length=100,
        examples=["local_file", "web", "confluence", "github"],
    )
    reference: str | None = Field(
        default=None,
        max_length=4_096,
        description=(
            "Provider-specific source reference, such as a file path, URL, "
            "repository, space, page, or resource identifier."
        ),
    )
    parameters: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Non-secret connector-specific parameters.",
    )


class IngestInput(HarborAPISchema):
    source: IngestSourceInput

    namespace: str = Field(
        default="default",
        min_length=1,
        max_length=255,
    )
    pipeline: str = Field(
        default="default",
        min_length=1,
        max_length=100,
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Prevents duplicate ingestion submissions.",
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class IngestionWorkflowInput(HarborAPISchema):
    """Submit one canonical Temporal ingestion workflow."""

    tenant_id: str = Field(min_length=1, max_length=255)
    connector_name: str = Field(min_length=1, max_length=100)
    run_id: str | None = Field(default=None, min_length=1, max_length=512)
    manifest_id: str | None = Field(default=None, min_length=1, max_length=512)
    generation_id: str | None = Field(default=None, min_length=1, max_length=512)
    max_artifacts: int | None = Field(default=None, ge=1)
    wait: bool = False


class IngestionControlInput(HarborAPISchema):
    """Apply a supported control action to a durable ingestion workflow."""

    action: Literal["pause", "resume", "cancel", "retry"]
    artifact_ids: list[str] = Field(default_factory=list, max_length=1_000)
    graceful: bool = True

    @model_validator(mode="after")
    def validate_artifact_ids(self) -> Self:
        if self.action == "retry" and not self.artifact_ids:
            raise ValueError("artifact_ids is required when action is retry")
        if self.action != "retry" and self.artifact_ids:
            raise ValueError("artifact_ids is only supported when action is retry")
        if any(not artifact_id.strip() for artifact_id in self.artifact_ids):
            raise ValueError("artifact_ids cannot contain blank values")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("artifact_ids cannot contain duplicate values")
        return self


class IngestResult(HarborAPISchema):
    documents_discovered: int = Field(default=0, ge=0)
    documents_processed: int = Field(default=0, ge=0)
    documents_failed: int = Field(default=0, ge=0)

    chunks_created: int = Field(default=0, ge=0)
    chunks_indexed: int = Field(default=0, ge=0)

    checkpoint_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    warnings: list[str] = Field(default_factory=list)


class RetrieveInput(HarborAPISchema):
    query: str = Field(
        min_length=1,
        max_length=32_000,
    )
    namespace: str = Field(
        default="default",
        min_length=1,
        max_length=255,
    )
    retrieval_profile: str = Field(
        default="default",
        min_length=1,
        max_length=100,
    )
    top_k: int = Field(default=10, ge=1, le=1_000)

    filters: dict[str, JsonValue] = Field(default_factory=dict)

    include_content: bool = True
    include_metadata: bool = True


class RetrievedItem(HarborAPISchema):
    chunk_id: str = Field(min_length=1, max_length=255)
    document_id: str = Field(min_length=1, max_length=255)

    content: str | None = None
    score: float

    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class RetrieveResult(HarborAPISchema):
    items: list[RetrievedItem] = Field(default_factory=list)
    retrieval_profile: str = Field(min_length=1, max_length=100)
    duration_ms: float = Field(ge=0)


class FetchOutput[TaskResultT](HarborAPISchema):
    task_id: str = Field(
        min_length=1,
        max_length=512,
        description="Public application task identifier.",
    )
    operation: TaskOperation
    status: TaskStatus

    result: TaskResultT | None = None
    message: str | None = Field(default=None, max_length=2_000)
    error: TaskError | None = None
    progress: TaskProgress | None = None

    created_at: AwareDatetime
    updated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_task_state(self) -> Self:  # noqa: C901 - invariants stay visible together
        terminal_statuses = {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }

        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")

        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at cannot be earlier than created_at")

        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot be earlier than created_at")

        if self.status is TaskStatus.SUCCEEDED:
            if self.result is None:
                raise ValueError("result is required when status is succeeded")

            if self.error is not None:
                raise ValueError("error must be empty when status is succeeded")

        elif self.result is not None:
            raise ValueError("result may only be populated when status is succeeded")

        if self.status is TaskStatus.FAILED and self.error is None:
            raise ValueError("error is required when status is failed")

        if self.status in terminal_statuses and self.completed_at is None:
            raise ValueError("completed_at is required for terminal task statuses")

        if self.status not in terminal_statuses and self.completed_at is not None:
            raise ValueError("completed_at must be empty for non-terminal task statuses")

        return self


class IngestOutput(FetchOutput[IngestResult]):
    operation: Literal[TaskOperation.INGEST] = TaskOperation.INGEST


class RetrieveOutput(FetchOutput[RetrieveResult]):
    operation: Literal[TaskOperation.RETRIEVE] = TaskOperation.RETRIEVE
