"""Strict connector-aware schemas for the public ingestion API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harborrag_app.api.schemas import ApiModel, VerbatimText
from harborrag_app.api.v1.admin.connection_schemas import IngestionOut


class IngestionStatus(StrEnum):
    """Upper-case states an ingestion run can be in."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IngestionPage(ApiModel):
    """One page of the tenant's ingestion runs, newest first."""

    items: list[IngestionOut]
    next_cursor: str | None = None


class IngestionListQuery(ApiModel):
    """Filters and paging of the ingestion run list."""

    project_id: str | None = None
    connection_ref: str | None = None
    status: Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"] | None = None
    created_after: AwareDatetime | None = None
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class IngestionDocumentItem(ApiModel):
    """One document of a run with its outcome and the stage it reached."""

    document_ref: VerbatimText
    document_id: str
    status: Literal["succeeded", "skipped", "failed"]
    stage: Literal["capture", "interpret", "commit", "derive", "publish"]
    error_category: str | None = None
    duration_ms: int | None = None


class IngestionDocumentPage(ApiModel):
    """One page of a run's succeeded, skipped and failed documents."""

    items: list[IngestionDocumentItem]
    next_cursor: str | None = None


class IngestionDocumentQuery(ApiModel):
    """Status filter and paging of a run's documents."""

    status: Literal["all", "succeeded", "skipped", "failed"] = "all"
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


FailureStage = Literal["capture", "interpret", "commit", "derive", "publish"]


class FailureOut(ApiModel):
    """One failed document of a run, as the failure inbox lists it."""

    failure_id: str
    ingestion_id: str
    connection_ref: str
    document_ref: VerbatimText
    stage: FailureStage
    error_category: str
    message: str
    attempts: int
    retryable: bool
    resolved: bool
    created_at: datetime


class FailurePage(ApiModel):
    """One page of failed documents, newest first, with counts per category."""

    items: list[FailureOut]
    next_cursor: str | None = None
    counts: dict[str, int]
    total: int | None = None


class FailureQuery(ApiModel):
    """Filters and paging of the failure inbox."""

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
    """Failures to retry, chosen by connection, category or stage."""

    connection_ref: str | None = None
    error_category: str | None = None
    stage: FailureStage | None = None


class FailureRetryRequest(ApiModel):
    """Failures to retry, by failure ID or by filter."""

    ids: list[Annotated[str, Field(max_length=400)]] | None = Field(
        default=None, min_length=1, max_length=1000
    )
    filter: FailureRetryFilter | None = None

    @model_validator(mode="after")
    def require_exactly_one_selector(self) -> FailureRetryRequest:
        """Require exactly one of ``ids`` and ``filter``."""
        if (self.ids is None) == (self.filter is None):
            raise ValueError("send exactly one of ids or filter")
        return self


class FailureRetryAccepted(ApiModel):
    """The retry runs started for the selected failures."""

    ingestion_id: str
    ingestion_ids: list[str]
    queued: int


class RetryFailuresRequest(ApiModel):
    """Failed documents of one run to retry, or every retryable one when empty."""

    document_ids: list[Annotated[str, Field(max_length=255)]] = Field(
        default_factory=list,
        max_length=1000,
    )

    @field_validator("document_ids")
    @classmethod
    def normalize_document_ids(cls, values: list[str]) -> list[str]:
        """Trim the document IDs, refuse blank ones and drop repeats in order."""
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("document IDs must be non-empty")
        return list(dict.fromkeys(normalized))
