"""Job aggregate: the domain-level view of an ingestion job.

Promoted from harborrag_runtime.job_state (arch plan target domain/job.py)
so core ports can type against it; the runtime JobState remains the thin
runner-side projection and re-exports JobStatus from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

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
        if not self.id or any(ch.isspace() for ch in self.id):
            raise ValueError("Job id must be non-empty and contain no whitespace.")
