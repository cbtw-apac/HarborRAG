"""The spec's ``Schedule`` object built from a stored schedule, its slots and their runs.

Pure: callers read the schedule, slots, runs and settings first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.config_store.durations import format_iso8601_duration
from harborrag_core.domain.schedule import Schedule, ScheduleSlot
from harborrag_core.ingestion import IngestionTask
from harborrag_engine.control_plane.connection_views import ingestion_status


def schedule_view(
    schedule: Schedule,
    *,
    slots: Sequence[ScheduleSlot],
    runs: Mapping[str, IngestionTask],
    enabled: bool,
) -> dict[str, Any]:
    """Return the spec's ``Schedule`` object.

    Args:
        schedule: The stored schedule.
        slots: Its recent slots, newest first.
        runs: The runs those slots name, by ingestion id; a run missing here reads as no run.
        enabled: ``schedules.enabled`` for the tenant; ``False`` empties ``next_run_at``.

    Returns:
        The ``Schedule`` object, ready to serialize.
    """
    return {
        "schedule_id": schedule.schedule_id,
        "description": schedule.description,
        "operation": schedule.operation,
        "input": {
            "project_id": schedule.project_id,
            "connection_id": schedule.connection_id,
            "mode": schedule.mode,
        },
        "connection_ref": schedule.connection_ref,
        "interval": (
            format_iso8601_duration(schedule.interval_seconds)
            if schedule.interval_seconds is not None
            else None
        ),
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "jitter": format_iso8601_duration(schedule.jitter_seconds),
        "if_running": schedule.if_running,
        "catch_up": schedule.catch_up,
        "pause_after_failures": schedule.pause_after_failures,
        "state": schedule.state,
        "paused": (
            {
                "reason": schedule.paused.reason,
                "by": schedule.paused.by,
                "at": schedule.paused.at,
            }
            if schedule.paused is not None
            else None
        ),
        "next_run_at": (schedule.next_run_at if enabled and schedule.paused is None else None),
        "consecutive_failures": schedule.consecutive_failures,
        "recent_slots": [_slot_view(slot, runs) for slot in slots],
        "created_by": schedule.created_by,
        "updated_by": schedule.updated_by,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def _slot_view(slot: ScheduleSlot, runs: Mapping[str, IngestionTask]) -> dict[str, Any]:
    """Return one ``recent_slots`` entry with its run's current status."""
    task = runs.get(slot.ingestion_id) if slot.ingestion_id is not None else None
    return {
        "due_at": slot.due_at,
        "outcome": slot.outcome,
        "reason": slot.reason,
        "manual": slot.manual,
        "run": (
            {"type": "ingestion", "id": task.task_id, "status": ingestion_status(task.status)}
            if task is not None
            else None
        ),
    }
