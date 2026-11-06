"""Strict connector-aware schemas for the public ingestion API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator

from harborrag_app.api.schemas import ApiModel
from harborrag_app.api.v1.admin.connection_schemas import IngestionOut


class IngestionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IngestionPage(ApiModel):
    items: list[IngestionOut]
    next_cursor: str | None = None


class IngestionListQuery(ApiModel):
    project_id: str | None = None
    connection_ref: str | None = None
    status: Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"] | None = None
    created_after: datetime | None = None
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


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
