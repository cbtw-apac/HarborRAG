"""The lease on a RUNNING projection cleanup, so a dead worker's claim expires.

``claim_cleanup`` is the only writer that moves a job to RUNNING, and it stamps
``updated_at``; nothing else writes a RUNNING row (enqueueing and publication reopen
only CANCELLED or COMPLETED jobs, and every other transition leaves RUNNING). So
``updated_at`` is the claim time and the lease needs no column of its own. Keep it that
way: a writer that refreshed ``updated_at`` on a RUNNING row would extend the lease.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, or_

from harborrag_core.ingestion import CleanupJobState

from .row_values import DatabaseRow, required_datetime, required_text
from .schema import PROJECTION_CLEANUP_JOBS

# Far above one version's vector and graph delete, which takes seconds.
CLEANUP_LEASE = timedelta(minutes=15)
_UNCLAIMED = (CleanupJobState.PENDING.value, CleanupJobState.FAILED.value)


def claimable_cleanup(now: datetime) -> ColumnElement[bool]:
    """Unclaimed jobs, and RUNNING jobs whose claim outlived the lease."""

    jobs = PROJECTION_CLEANUP_JOBS.c
    return or_(
        jobs.status.in_(_UNCLAIMED),
        and_(
            jobs.status == CleanupJobState.RUNNING.value,
            jobs.updated_at <= now - CLEANUP_LEASE,
        ),
    )


def cleanup_in_flight(row: DatabaseRow, now: datetime) -> bool:
    """A live claim: RUNNING and claimed within the lease."""

    return (
        required_text(row, "status") == CleanupJobState.RUNNING.value
        and required_datetime(row, "updated_at") > now - CLEANUP_LEASE
    )


def cleanup_claimable(row: DatabaseRow, now: datetime) -> bool:
    status = required_text(row, "status")
    if status in _UNCLAIMED:
        return True
    return status == CleanupJobState.RUNNING.value and not cleanup_in_flight(row, now)
