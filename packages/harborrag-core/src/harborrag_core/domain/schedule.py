"""Schedule aggregate: one connection operation run on an interval or a cron timetable.

A schedule belongs to a tenant and names the connection its slots act on. Its slots
call the same operation as the matching endpoint, so this module holds values only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from harborrag_core.base import utc_now

from .connection import CONNECTION_ID_PATTERN
from .validation import require_tenant_id

ScheduleOperation = Literal["start_ingestion"]
ScheduleMode = Literal["incremental", "force"]
IfRunning = Literal["skip", "queue"]
ScheduleState = Literal["active", "paused"]
SlotOutcome = Literal["started", "skipped", "queued"]
SlotReason = Literal[
    "already_running", "disabled_by_setting", "connection_deleting", "reference_not_found"
]

SCHEDULE_ID_PATTERN = CONNECTION_ID_PATTERN
RECENT_SLOT_LIMIT = 20
QUEUE_WAIT_LIMIT = timedelta(hours=24)
SYSTEM_PAUSER = "system"


@dataclass(frozen=True, slots=True)
class SchedulePause:
    """Why and by whom a schedule was paused.

    Args:
        reason: Text an operator gave, or the automatic pause's explanation.
        by: Subject that paused it, or ``system`` for an automatic pause.
        at: When it was paused.
    """

    reason: str
    by: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Schedule:
    """A connection operation and the timetable that runs it.

    Exactly one of ``interval_seconds`` and ``cron`` is set.

    Args:
        tenant_id: Owning tenant.
        schedule_id: ID the caller chose, unique in the tenant.
        project_id: Project of the connection each slot acts on.
        connection_id: Connection each slot acts on.
        operation: Operation each slot calls.
        mode: Ingestion mode each slot asks for.
        description: Free text shown to operators.
        interval_seconds: Seconds between slots.
        cron: Five-field cron expression.
        timezone: IANA time zone the cron expression runs in.
        jitter_seconds: Random delay of up to this long before each slot.
        if_running: ``skip`` drops a slot while the connection has a run; ``queue``
            starts one slot when that run ends.
        catch_up: Run once for a slot missed during downtime.
        pause_after_failures: Pause after this many failed runs in a row; 0 never pauses.
        paused: Pause details; ``None`` while active.
        consecutive_failures: Failed runs in a row since the last success or resume.
        next_run_at: Next slot as last read from the timer; ``None`` while paused.
        version: Revision behind the ETag; config, pause and resume writes advance it.
        created_by: Subject that created it.
        updated_by: Subject that last changed it.
        created_at: Creation time.
        updated_at: Last change time.
    """

    tenant_id: str
    schedule_id: str
    project_id: str
    connection_id: str
    operation: ScheduleOperation = "start_ingestion"
    mode: ScheduleMode = "incremental"
    description: str | None = None
    interval_seconds: int | None = None
    cron: str | None = None
    timezone: str = "UTC"
    jitter_seconds: int = 0
    if_running: IfRunning = "skip"
    catch_up: bool = False
    pause_after_failures: int = 3
    paused: SchedulePause | None = None
    consecutive_failures: int = 0
    next_run_at: datetime | None = None
    version: int = 1
    created_by: str = ""
    updated_by: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_tenant_id(self.tenant_id)
        if not SCHEDULE_ID_PATTERN.fullmatch(self.schedule_id):
            raise ValueError("schedule_id must match ^[a-z0-9][a-z0-9_-]{0,62}$")
        if (self.interval_seconds is None) == (self.cron is None):
            raise ValueError("a schedule has exactly one of interval_seconds and cron")

    @property
    def state(self) -> ScheduleState:
        """``paused`` while a pause is recorded, else ``active``."""
        return "paused" if self.paused is not None else "active"

    @property
    def connection_ref(self) -> str:
        """The connection as ``project_id/connection_id``."""
        return f"{self.project_id}/{self.connection_id}"


@dataclass(frozen=True, slots=True)
class ScheduleSlot:
    """One time a schedule came due, and what it did.

    Args:
        due_at: When the slot came due, or when ``trigger`` ran it.
        outcome: ``started``, ``skipped`` or ``queued``.
        reason: Why the slot was skipped or queued; ``None`` when it started, or when
            the run's own checks refused it.
        manual: Whether ``trigger`` started it.
        ingestion_id: The run it started, or the run that made it skip or wait.
    """

    due_at: datetime
    outcome: SlotOutcome
    reason: SlotReason | None = None
    manual: bool = False
    ingestion_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleFilter:
    """Filters for listing a tenant's schedules; ``None`` leaves a field unfiltered.

    Args:
        project_id: Keep schedules of this project.
        connection_id: Keep schedules of this connection; set with ``project_id``.
        operation: Keep schedules that call this operation.
        state: Keep active or paused schedules.
        due_before: Keep schedules whose ``next_run_at`` is earlier.
    """

    project_id: str | None = None
    connection_id: str | None = None
    operation: ScheduleOperation | None = None
    state: ScheduleState | None = None
    due_before: datetime | None = None
