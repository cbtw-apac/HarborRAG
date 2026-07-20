"""Job queue port (plan ST6): the seam Temporal replaces in Phase 7.

M0 ships the Protocol + fake only; the SQLAlchemy queue with real
lease/retry semantics is M2 (ported from qdrant-loader 1.0.3).
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
