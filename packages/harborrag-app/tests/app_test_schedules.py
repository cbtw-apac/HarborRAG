"""Schedule-service double with the runtime service's signatures, over an in-memory store."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from harborrag_core.contracts.errors import (
    HarborPreconditionFailedError,
    ScheduleIdTakenError,
    ScheduleNotFoundError,
)
from harborrag_core.domain.schedule import Schedule, SchedulePause
from harborrag_engine.control_plane.connection_views import ANY_VERSION
from harborrag_runtime.application.control_plane.schedule_models import (
    ScheduleKey,
    ScheduleQuery,
    ScheduleWrite,
    WriteConditions,
)
from harborrag_runtime.application.control_plane.schedule_rules import parse_timetable
from harborrag_runtime.application.control_plane.schedule_views import schedule_view

STAMP = datetime(2026, 12, 1, 3, tzinfo=UTC)
MIN_INTERVAL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class ScheduleCall:
    """One call made to the schedule service.

    Args:
        name: The service method.
        arguments: Its arguments by name.
    """

    name: str
    arguments: dict[str, object]


class FakeScheduleService:
    """``ConnectionScheduleService`` stand-in that stores schedules and records every call.

    ``put`` checks the timetable with the real rules and a 15-minute minimum, but no
    connection; nothing fires slots.

    Attributes:
        schedules: Stored schedules by key.
        calls: Every call, in order.
        error: Raised by every call instead of answering, when set.
    """

    def __init__(self) -> None:
        self.schedules: dict[ScheduleKey, Schedule] = {}
        self.calls: list[ScheduleCall] = []
        self.error: BaseException | None = None

    async def page(self, tenant_id: str, query: ScheduleQuery) -> dict[str, Any]:
        self._record("page", tenant_id=tenant_id, query=query)
        items = [
            _view(schedule)
            for key, schedule in self.schedules.items()
            if key.tenant_id == tenant_id
        ]
        return {"items": items[: query.limit], "next_cursor": None}

    async def get(self, key: ScheduleKey) -> tuple[dict[str, Any], int]:
        self._record("get", key=key)
        schedule = self._require(key)
        return _view(schedule), schedule.version

    async def put(
        self, key: ScheduleKey, write: ScheduleWrite, *, actor: str, conditions: WriteConditions
    ) -> tuple[dict[str, Any], int, bool]:
        self._record("put", key=key, write=write, actor=actor, conditions=conditions)
        timetable = parse_timetable(write, min_interval_seconds=MIN_INTERVAL_SECONDS)
        current = self._check(key, conditions)
        stored = Schedule(
            tenant_id=key.tenant_id,
            schedule_id=key.schedule_id,
            project_id=write.project_id,
            connection_id=write.connection_id,
            mode=write.mode,
            description=write.description,
            interval_seconds=timetable.interval_seconds,
            cron=timetable.cron,
            timezone=timetable.timezone,
            jitter_seconds=timetable.jitter_seconds,
            if_running=write.if_running,
            catch_up=write.catch_up,
            pause_after_failures=write.pause_after_failures,
            paused=current.paused if current is not None else None,
            version=current.version + 1 if current is not None else 1,
            created_by=current.created_by if current is not None else actor,
            updated_by=actor,
            created_at=STAMP,
            updated_at=STAMP,
        )
        self.schedules[key] = stored
        return _view(stored), stored.version, current is None

    async def delete(self, key: ScheduleKey) -> None:
        self._record("delete", key=key)
        self._require(key)
        del self.schedules[key]

    async def pause(self, key: ScheduleKey, *, reason: str | None, actor: str) -> dict[str, Any]:
        self._record("pause", key=key, reason=reason, actor=actor)
        schedule = self._require(key)
        if schedule.paused is None:
            pause = SchedulePause(reason=reason or "Paused by an operator", by=actor, at=STAMP)
            schedule = self._save(key, replace(schedule, paused=pause, updated_by=actor))
        return _view(schedule)

    async def resume(self, key: ScheduleKey, *, actor: str) -> dict[str, Any]:
        self._record("resume", key=key, actor=actor)
        schedule = self._require(key)
        resumed = replace(schedule, paused=None, consecutive_failures=0, updated_by=actor)
        return _view(self._save(key, resumed))

    async def trigger(self, key: ScheduleKey) -> dict[str, Any]:
        self._record("trigger", key=key)
        schedule = self._require(key)
        return {
            "ingestion_id": "ing_93",
            "tenant_id": key.tenant_id,
            "project_id": schedule.project_id,
            "connection_id": schedule.connection_id,
            "mode": schedule.mode,
            "status": "queued",
            "counters": {"discovered": 0, "processed": 0, "skipped": 0, "failed": 0},
            "stages": [],
            "error_category": None,
            "triggered_by": {"principal_type": "schedule", "principal_id": key.schedule_id},
            "settings_revision": "rev_1",
            "workflow_id": "harborrag-source:ing_93",
            "run_id": "ing_93",
            "trace_id": "",
            "links": {"temporal": "", "grafana": "", "langfuse": ""},
            "created_at": STAMP,
            "finished_at": None,
        }

    def _record(self, name: str, **arguments: object) -> None:
        self.calls.append(ScheduleCall(name, arguments))
        if self.error is not None:
            raise self.error

    def _require(self, key: ScheduleKey) -> Schedule:
        schedule = self.schedules.get(key)
        if schedule is None:
            raise ScheduleNotFoundError(f"schedule {key.schedule_id!r} not found")
        return schedule

    def _check(self, key: ScheduleKey, conditions: WriteConditions) -> Schedule | None:
        """Hold ``If-None-Match`` and ``If-Match`` against the stored schedule."""
        current = self.schedules.get(key)
        if current is not None and conditions.create_only:
            raise ScheduleIdTakenError(f"schedule {key.schedule_id!r} already exists")
        expected = conditions.expected_version
        if expected is not None and (
            current is None or (expected != ANY_VERSION and expected != current.version)
        ):
            raise HarborPreconditionFailedError("the schedule changed since it was read")
        return current

    def _save(self, key: ScheduleKey, schedule: Schedule) -> Schedule:
        saved = replace(schedule, version=schedule.version + 1)
        self.schedules[key] = saved
        return saved


def _view(schedule: Schedule) -> dict[str, Any]:
    return schedule_view(schedule, slots=[], runs={}, enabled=True)
