"""Framework-independent ingestion job aggregate used by core ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .validation import require_id

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
JobType = Literal["bulk_ingest", "incremental_pull", "dry_run"]


def _utc_now() -> datetime:
    """Timezone-aware UTC timestamp (RFC 3339-ready, per API conventions)."""
    return datetime.now(UTC)


@dataclass(slots=True)
class JobCounters:
    """Progress counters surfaced on the Job API resource."""

    documents_processed: int = 0
    chunks_created: int = 0
    errors: int = 0


@dataclass(slots=True)
class Job:
    """A single ingestion job: identity, lifecycle status, and progress.

    Timestamps are timezone-aware UTC; `payload` carries job-type-specific
    parameters and must never contain secret values.
    """

    id: str
    source_id: str
    project_id: str
    job_type: JobType
    status: JobStatus = "queued"
    dry_run: bool = False
    attempts: int = 0
    enqueued_at: datetime = field(default_factory=_utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counters: JobCounters = field(default_factory=JobCounters)
    last_error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_id(self.id, label="Job")
