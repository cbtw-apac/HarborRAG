"""Strict connector-aware schemas for the public ingestion API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harborrag_app.operations.schemas.common import ApiModel
from harborrag_app.operations.schemas.connections import IngestionOut


class IngestionStatus(StrEnum):
    """Lifecycle states of an ingestion run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IngestionPage(ApiModel):
    """One page of ingestions."""

    items: list[IngestionOut]
    next_cursor: str | None = None


class IngestionListQuery(ApiModel):
    """Filters for listing ingestions."""

    project_id: str | None = None
    connection_ref: str | None = None
    status: Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"] | None = None
    created_after: AwareDatetime | None = None
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class IngestionDocumentItem(ApiModel):
    """One document of an ingestion and its outcome."""

    document_ref: str
    document_id: str
    status: Literal["succeeded", "skipped", "failed"]
    stage: Literal["capture", "interpret", "commit", "derive", "publish"]
    error_category: str | None = None
    duration_ms: int | None = None


class IngestionDocumentPage(ApiModel):
    """One page of an ingestion's documents."""

    items: list[IngestionDocumentItem]
    next_cursor: str | None = None


class IngestionDocumentQuery(ApiModel):
    """Filter and paging for an ingestion's documents."""

    status: Literal["all", "succeeded", "skipped", "failed"] = "all"
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


FailureStage = Literal["capture", "interpret", "commit", "derive", "publish"]


class FailureOut(ApiModel):
    """One failed document of an ingestion."""

    failure_id: str
    ingestion_id: str
    connection_ref: str
    document_ref: str
    stage: FailureStage
    error_category: str
    message: str
    attempts: int
    retryable: bool
    resolved: bool
    created_at: datetime


class FailurePage(ApiModel):
    """One page of failures with counts per category."""

    items: list[FailureOut]
    next_cursor: str | None = None
    counts: dict[str, int]
    total: int | None = None


class FailureQuery(ApiModel):
    """Filters for listing failures."""

    project_id: str | None = None
    connection_ref: str | None = None
    stage: FailureStage | None = None
    error_category: str | None = None
    resolved: bool = False
    q: str | None = Field(default=None, max_length=200)
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    include_total: bool = False


class FailureRetryFilter(ApiModel):
    """Selects failures to retry by connection, error category or stage."""

    connection_ref: str | None = None
    error_category: str | None = None
    stage: FailureStage | None = None


class FailureRetryRequest(ApiModel):
    """Failures to retry, chosen by IDs or by a filter."""

    ids: list[Annotated[str, Field(max_length=400)]] | None = Field(
        default=None, min_length=1, max_length=1000
    )
    filter: FailureRetryFilter | None = None

    @model_validator(mode="after")
    def require_exactly_one_selector(self) -> FailureRetryRequest:
        """Reject a request that sends both or neither of ``ids`` and ``filter``."""
        if (self.ids is None) == (self.filter is None):
            raise ValueError("send exactly one of ids or filter")
        return self


class FailureRetryAccepted(ApiModel):
    """Ingestions queued by a failure retry."""

    ingestion_id: str
    ingestion_ids: list[str]
    queued: int


class RetryFailuresRequest(ApiModel):
    """Documents of one ingestion to retry."""

    document_ids: list[Annotated[str, Field(max_length=255)]] = Field(
        default_factory=list,
        max_length=1000,
    )

    @field_validator("document_ids")
    @classmethod
    def normalize_document_ids(cls, values: list[str]) -> list[str]:
        """Strip document IDs and reject empty ones."""
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("document IDs must be non-empty")
        return list(dict.fromkeys(normalized))
