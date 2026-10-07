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
    # None leaves an existing schedule's pause state alone; creation treats it as False.
    paused: bool | None = None
    note: str | None = None
