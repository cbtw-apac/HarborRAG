"""Inputs, results and the timer seam of the schedule use cases.

Nothing here does I/O. ``ScheduleTimers`` is the seam to the engine that fires slots;
the Temporal implementation lives in ``harborrag_runtime.temporal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from harborrag_core.domain.schedule import IfRunning, Schedule, ScheduleFilter, ScheduleMode

SlotStartOutcome = Literal["started", "skipped", "queued", "ignored"]


@dataclass(frozen=True, slots=True)
class ScheduleKey:
    """A schedule's identity.

    Args:
        tenant_id: Owning tenant.
        schedule_id: ID the caller chose, unique in the tenant.
    """

    tenant_id: str
    schedule_id: str


@dataclass(frozen=True, slots=True)
class ScheduleWrite:
    """A schedule's configuration as the caller sent it; ``PUT`` checks it.

    Args:
        project_id: Project of the connection each slot acts on.
        connection_id: Connection each slot acts on.
        mode: Ingestion mode each slot asks for.
        description: Free text shown to operators.
        interval: ISO 8601 duration between slots; set this or ``cron``.
        cron: Five-field cron expression; set this or ``interval``.
        timezone: IANA time zone the cron expression runs in.
        jitter: ISO 8601 duration of the random delay before each slot.
        if_running: ``skip`` or ``queue`` a slot that comes while the connection has a run.
        catch_up: Run once for a slot missed during downtime.
        pause_after_failures: Pause after this many failed runs in a row; 0 never pauses.
    """

    project_id: str
    connection_id: str
    mode: ScheduleMode = "incremental"
    description: str | None = None
    interval: str | None = None
    cron: str | None = None
    timezone: str = "UTC"
    jitter: str = "PT0S"
    if_running: IfRunning = "skip"
    catch_up: bool = False
    pause_after_failures: int = 3


@dataclass(frozen=True, slots=True)
class WriteConditions:
    """Preconditions of a write, from ``If-Match`` and ``If-None-Match``.

    Args:
        expected_version: Version the caller last read; ``ANY_VERSION`` accepts any
            existing version, and ``None`` skips the check.
        create_only: Refuse to overwrite an existing schedule.
    """

    expected_version: int | None = None
    create_only: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleQuery:
    """One page request of a tenant's schedules.

    Args:
        filters: Fields to match.
        limit: Page size.
        cursor: ``next_cursor`` from the previous page.
    """

    filters: ScheduleFilter
    limit: int
    cursor: str | None = None


class SlotAttempt(StrEnum):
    """Whether a slot runs when it comes due or after waiting for a run to end."""

    DUE = "due"
    AFTER_WAIT = "after_wait"


@dataclass(frozen=True, slots=True)
class SlotStart:
    """What one slot did.

    Args:
        outcome: ``started``, ``skipped``, ``queued`` to wait for a run, or ``ignored``
            when the schedule is gone or paused.
        ingestion_id: The run it started, or the run it skipped or waits behind.
    """

    outcome: SlotStartOutcome
    ingestion_id: str | None = None


class ScheduleTimers(Protocol):
    """Fires each schedule's slots on its timetable.

    A timer carries only the schedule's identity; each slot reads everything else
    from the stored schedule. Every method is safe to repeat.
    """

    async def put(self, schedule: Schedule, *, catch_up_window_seconds: int) -> datetime | None:
        """Create or replace the timer with the schedule's timetable and pause state.

        Args:
            schedule: The stored schedule.
            catch_up_window_seconds: How late a slot missed during downtime may still fire.

        Returns:
            When the next slot fires; ``None`` while paused.

        Raises:
            HarborUnavailableError: The timer engine cannot be reached.
        """
        ...

    async def pause(self, tenant_id: str, schedule_id: str, *, note: str) -> None:
        """Stop firing slots; a missing timer is a no-op."""
        ...

    async def next_run_at(self, tenant_id: str, schedule_id: str) -> datetime | None:
        """When the next slot fires; ``None`` while paused or when the timer is missing."""
        ...

    async def delete(self, tenant_id: str, schedule_id: str) -> None:
        """Remove the timer; a missing timer is a no-op."""
        ...

    async def follow(self, tenant_id: str, schedule_id: str, ingestion_id: str) -> None:
        """Watch a run ``trigger`` started, so its outcome counts toward the auto-pause."""
        ...
