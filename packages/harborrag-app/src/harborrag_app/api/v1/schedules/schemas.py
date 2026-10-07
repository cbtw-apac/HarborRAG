"""HTTP contracts for application-owned Temporal schedules."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from harborrag_app.api.schemas import ApiModel


class ScheduleSourceRequest(ApiModel):
    tenant: str = Field(default="DEFAULT", min_length=1, max_length=128)
    connection_id: str = Field(min_length=1, max_length=255)
    source_scope_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["incremental", "force"] = "incremental"


class ScheduleUpsertRequest(ApiModel):
    schedule_id: str = Field(min_length=1, max_length=128)
    workflow: Literal["source_ingestion"]
    cron: str | None = Field(default=None, min_length=1, max_length=255)
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    overlap: Literal[
        "skip",
        "buffer_one",
        "buffer_all",
        "cancel_other",
        "terminate_other",
        "allow_all",
    ] = "skip"
    catchup_window_seconds: int = Field(default=3600, ge=10)
    jitter_seconds: int | None = Field(default=None, ge=1)
    pause_on_failure: bool = False
    source: ScheduleSourceRequest
    # Omit on PATCH to keep the current pause state; creation defaults to unpaused.
    paused: bool | None = None
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_schedule_spec(self) -> ScheduleUpsertRequest:
        if self.cron is None and self.interval_seconds is None:
            raise ValueError("provide cron, interval_seconds, or both")
        return self


class ScheduleActionRequest(ApiModel):
    note: str | None = Field(default=None, max_length=1000)


class ScheduleBackfillRequest(ApiModel):
    start_at: datetime
    end_at: datetime


class ScheduleSourceResponse(ApiModel):
    tenant: str
    connection_id: str
    source_scope_id: str | None = None
    mode: Literal["incremental", "force"]


class ScheduleRunResponse(ApiModel):
    scheduled_at: datetime
    started_at: datetime


class ScheduleResponse(ApiModel):
    schedule_id: str
    workflow: str
    cron: str | None
    interval_seconds: int | None = None
    timezone: str
    overlap: str
    catchup_window_seconds: int = 3600
    jitter_seconds: int | None = None
    pause_on_failure: bool = False
    paused: bool
    note: str | None = None
    managed_by: str
    source: ScheduleSourceResponse
    next_run_times: list[datetime]
    recent_runs: list[ScheduleRunResponse]
    total_runs: int | None = None
    skipped_overlap_runs: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScheduleListResponse(ApiModel):
    items: list[ScheduleResponse]


class ScheduleActionResponse(ApiModel):
    schedule_id: str
    message: str
