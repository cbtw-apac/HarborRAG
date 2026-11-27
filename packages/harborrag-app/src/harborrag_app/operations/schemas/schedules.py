"""Request and response schemas of the ``/v1/admin/schedules`` operations.

Imports no web framework. Shape checks live here; timetable rules that depend on
the tenant's settings are checked by the schedule service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from harborrag_app.operations.schemas.common import ApiModel
from harborrag_app.operations.schemas.connections import ID_PATTERN

ScheduleOperationName = Literal["start_ingestion"]
ScheduleStateName = Literal["active", "paused"]


class ScheduleInputIn(ApiModel):
    """The body of ``POST /v1/admin/ingestions`` each slot sends.

    Scheduled ingestions refuse ``item_refs`` and ``updated_after``.
    """

    project_id: str = Field(min_length=1)
    connection_id: str = Field(min_length=1)
    mode: Literal["incremental", "force"] = Field("incremental", description="start_ingestion only")


class ScheduleWriteRequest(ApiModel):
    """A schedule's configuration; exactly one of ``interval`` and ``cron``."""

    description: str | None = Field(None, max_length=200)
    operation: ScheduleOperationName = Field(
        "start_ingestion",
        description="The operation each slot calls, through the same code path as its endpoint",
    )
    input: ScheduleInputIn = Field(
        description="The body that operation takes, checked against its schema"
    )
    interval: str | None = Field(
        None,
        description="ISO 8601 duration, at least schedules.min_interval (PT15M by default)",
    )
    cron: str | None = Field(
        None,
        description="Five-field cron expression; no gap between runs may be under "
        "schedules.min_interval",
    )
    timezone: str = Field("UTC", description="IANA time zone for cron")
    jitter: str = Field(
        "PT0S",
        description="Random delay up to this long before each slot; at most half the "
        "interval and PT1H",
    )
    if_running: Literal["skip", "queue"] = Field(
        "skip",
        description="When the connection already has a run: skip drops the slot, queue "
        "starts it when that run ends (one slot waits at most)",
    )
    catch_up: bool = Field(
        False,
        description="After downtime, run once for a slot missed within the last interval "
        "(24 h at most)",
    )
    pause_after_failures: int = Field(
        3,
        ge=0,
        le=20,
        description="Pause after this many failed runs in a row; 0 never pauses",
    )

    @model_validator(mode="after")
    def _one_timetable(self) -> ScheduleWriteRequest:
        """Refuse a body with both or neither of ``interval`` and ``cron``."""
        if (self.interval is None) == (self.cron is None):
            raise ValueError("set exactly one of interval and cron")
        return self


class SchedulePauseRequest(ApiModel):
    """Optional body of the schedule actions; only ``pause`` reads it."""

    reason: str | None = Field(
        None, max_length=200, description="pause only; shown in paused.reason"
    )


class ScheduleListQuery(ApiModel):
    """Filters and paging for listing schedules."""

    project_id: str | None = None
    connection_ref: str | None = Field(None, description="Connection ref: project_id/connection_id")
    operation: ScheduleOperationName | None = Field(
        None, description="The operation each slot calls"
    )
    state: ScheduleStateName | None = None
    due_before: AwareDatetime | None = Field(
        None, description="Only schedules whose next_run_at is earlier"
    )
    cursor: str | None = Field(None, description="next_cursor from the previous page")
    limit: int = Field(default=20, ge=1, le=100, description="Page size")


class ScheduleInputOut(BaseModel):
    """The input each slot sends to its operation."""

    project_id: str
    connection_id: str
    mode: Literal["incremental", "force"]


class SchedulePausedOut(BaseModel):
    """Why, by whom and when a schedule was paused."""

    reason: str
    by: str = Field(description="Who paused it, or system for an automatic pause")
    at: datetime


class SlotRunOut(BaseModel):
    """The run a slot started, or the one that made it skip or wait."""

    type: Literal["ingestion"]
    id: str
    status: str


class ScheduleSlotOut(BaseModel):
    """One time a schedule came due, and what it did."""

    due_at: datetime
    outcome: Literal["started", "skipped", "queued"]
    reason: (
        Literal[
            "already_running", "disabled_by_setting", "connection_deleting", "reference_not_found"
        ]
        | None
    ) = Field(description="Why the slot was skipped or queued")
    manual: bool = Field(description="true when POST …/trigger started it")
    run: SlotRunOut | None


class ScheduleOut(BaseModel):
    """One schedule: which operation it calls with which input, and on what timetable."""

    schedule_id: str = Field(pattern=ID_PATTERN, description="Chosen by you; unique in the tenant")
    description: str | None
    operation: ScheduleOperationName
    input: ScheduleInputOut
    connection_ref: str = Field(description="project_id/connection_id from input")
    interval: str | None = Field(description="ISO 8601 duration; null when cron is set")
    cron: str | None = Field(description="Five-field cron expression; null when interval is set")
    timezone: str = Field(description="IANA time zone the cron expression runs in")
    jitter: str
    if_running: Literal["skip", "queue"]
    catch_up: bool
    pause_after_failures: int
    state: ScheduleStateName
    paused: SchedulePausedOut | None
    next_run_at: datetime | None = Field(
        description="null while paused or while schedules.enabled is false"
    )
    consecutive_failures: int
    recent_slots: list[ScheduleSlotOut] = Field(
        description="Newest first: the last 20 slots, including skipped ones. Lists carry "
        "only the latest"
    )
    created_by: str = Field(description="Read from the token")
    updated_by: str = Field(description="Read from the token")
    created_at: datetime
    updated_at: datetime


class SchedulePage(BaseModel):
    """One page of schedules, newest first."""

    items: list[ScheduleOut]
    next_cursor: str | None = Field(
        description="Pass as cursor to get the next page; null on the last page"
    )
