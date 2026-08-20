"""Strict connector-aware schemas for the public ingestion API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator

from harborrag_app.api.schemas import ApiModel


class IngestionMode(StrEnum):
    """Control whether unchanged source records may be skipped."""

    INCREMENTAL = "incremental"
    FORCE = "force"


class IngestionCreateRequest(ApiModel):
    connection_id: str = Field(
        min_length=1,
        max_length=255,
        description="Enabled connector name from config/connectors.yaml.",
    )
    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Tenant projection namespace; defaults to DEFAULT.",
    )
    mode: IngestionMode = Field(
        default=IngestionMode.INCREMENTAL,
        description=(
            "incremental skips unchanged documents; force fetches and evaluates "
            "unchanged documents again without deleting data or forcing a reindex"
        ),
    )


class IngestionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IngestionStage(StrEnum):
    QUEUED = "QUEUED"
    DISCOVERING = "DISCOVERING"
    PROCESSING_DOCUMENTS = "PROCESSING_DOCUMENTS"
    RECONCILING = "RECONCILING"
    COMPLETED = "COMPLETED"


class SourceSummary(ApiModel):
    type: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    connection_id: str


class IngestionProgress(ApiModel):
    discovered: int = Field(default=0, ge=0)
    admitted: int = Field(default=0, ge=0)
    processed: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)


class IngestionTaskResponse(ApiModel):
    task_id: str
    tenant: str
    status: IngestionStatus
    stage: IngestionStage
    source: SourceSummary
    progress: IngestionProgress
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    message: str | None = None


class IngestionTaskPage(ApiModel):
    items: list[IngestionTaskResponse]
    next_cursor: str | None = None


class IngestionTaskQuery(ApiModel):
    tenant: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Restrict the listing to one tenant the caller may read.",
    )
    status: IngestionStatus | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class DocumentResultStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    REMOVED = "REMOVED"
    CANCELLED = "CANCELLED"


class DocumentFailureStage(StrEnum):
    FETCH = "FETCH"
    PARSE = "PARSE"
    CANONICALIZE = "CANONICALIZE"
    CHUNK = "CHUNK"
    ENCODE = "ENCODE"
    VECTOR_INDEX = "VECTOR_INDEX"
    GRAPH_INDEX = "GRAPH_INDEX"
    VERIFY = "VERIFY"
    PUBLISH = "PUBLISH"


class DocumentFailure(ApiModel):
    code: str
    message: str
    stage: DocumentFailureStage
    retryable: bool


class IngestionDocumentResult(ApiModel):
    document_id: str
    source_item_id: str
    document_kind: str
    title: str | None = None
    status: DocumentResultStatus
    active_document_version_id: str | None = None
    failure: DocumentFailure | None = None
    updated_at: datetime


class IngestionDocumentPage(ApiModel):
    items: list[IngestionDocumentResult]
    next_cursor: str | None = None


class IngestionDocumentQuery(ApiModel):
    status: DocumentResultStatus | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class RetryFailuresRequest(ApiModel):
    document_ids: list[Annotated[str, Field(max_length=255)]] = Field(
        default_factory=list,
        max_length=1000,
    )

    @field_validator("document_ids")
    @classmethod
    def normalize_document_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("document IDs must be non-empty")
        return list(dict.fromkeys(normalized))


class IngestionAcceptedResponse(ApiModel):
    task_id: str
    status: Literal["PENDING"]
    message: str
    submitted_at: datetime


class IngestionActionResponse(ApiModel):
    task_id: str
    status: IngestionStatus
    message: str


class RetryAcceptedResponse(ApiModel):
    task_id: str
    retry_task_id: str
    accepted_document_count: int = Field(ge=1)
    message: str
