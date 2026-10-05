"""Transport-neutral schedule commands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScheduleCommand:
    schedule_id: str
    workflow: str
    cron: str | None
    timezone: str
    overlap: str
    tenant_id: str
    connection_id: str
    source_scope_id: str | None = None
    interval_seconds: int | None = None
    catchup_window_seconds: int = 3600
    jitter_seconds: int | None = None
    pause_on_failure: bool = False
    force_reprocess: bool = False
    paused: bool = False
    note: str | None = None
