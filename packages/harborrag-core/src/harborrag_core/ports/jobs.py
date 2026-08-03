"""Job queue port (plan ST6): the seam Temporal replaces in Phase 7.

M0 shipped the Protocol only, with a SQLAlchemy queue (lease/retry semantics
ported from qdrant-loader 1.0.3) planned for M2. That plan changed: M2 instead
bridges persisted Job rows directly onto the Temporal ingestion path that
already existed in this codebase (see workflow_control.jobs.JobsMixin), since
Temporal already provides durable execution, retry, and replay -- a second,
SQL-backed queue running the same jobs would duplicate that. This Protocol
has no adapter and nothing in the app constructs one; it stays defined for a
possible future non-Temporal job runtime, not as a gap to fill.
"""

from __future__ import annotations

from typing import Protocol

from harborrag_core.domain.job import Job


class JobQueuePort(Protocol):
    """Durable FIFO of jobs with atomic claim, retry, and cancel."""

    async def enqueue(self, job: Job) -> Job:
        """Add a queued job; returns the persisted job."""

    async def claim_next(self, lease_seconds: int) -> Job | None:
        """Atomically claim the oldest queued job (status -> running,
        attempts += 1) or return None when the queue is empty."""

    async def mark_done(self, job_id: str) -> None:
        """Mark a running job succeeded."""

    async def mark_failed(self, job_id: str, error: str, retryable: bool) -> None:
        """Record the error; re-queue when retryable, else mark failed."""

    async def cancel(self, job_id: str) -> None:
        """Cancel a job; cancelled jobs are never claimable."""
