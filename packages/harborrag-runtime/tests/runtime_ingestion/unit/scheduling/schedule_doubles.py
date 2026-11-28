"""In-memory stand-ins for the schedule use cases' repository, timers, starter and runs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.resolution import EffectiveSettings, EffectiveValue
from harborrag_core.contracts.errors import (
    HarborPreconditionFailedError,
    HarborUnavailableError,
    ScheduleIdTakenError,
)
from harborrag_core.domain.schedule import (
    RECENT_SLOT_LIMIT,
    Schedule,
    ScheduleFilter,
    SchedulePause,
    ScheduleSlot,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_runtime.application.control_plane.connection_models import ConnectionKey

TENANT = "acme"
NEXT_RUN = datetime(2026, 11, 28, 2, 0, tzinfo=UTC)


class FakeSchedules:
    """``ScheduleRepositoryPort`` in memory, following the port's version rules."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Schedule] = {}
        self.slots: dict[tuple[str, str], list[ScheduleSlot]] = {}
        self.waiting: set[tuple[str, str]] = set()

    async def get(self, *, tenant_id: str, schedule_id: str) -> Schedule | None:
        return self.rows.get((tenant_id, schedule_id))

    async def page(
        self, *, tenant_id: str, filters: ScheduleFilter, limit: int, cursor: str | None
    ) -> tuple[list[Schedule], str | None]:
        rows = [row for (tenant, _), row in self.rows.items() if tenant == tenant_id]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)[:limit], None

    async def list_for_connection(
        self, *, tenant_id: str, project_id: str, connection_id: str
    ) -> list[Schedule]:
        return [
            row
            for row in self.rows.values()
            if (row.tenant_id, row.project_id, row.connection_id)
            == (tenant_id, project_id, connection_id)
        ]

    async def next_runs(
        self, *, tenant_id: str, connections: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], datetime]:
        wanted = set(connections)
        earliest: dict[tuple[str, str], datetime] = {}
        for row in self.rows.values():
            key = (row.project_id, row.connection_id)
            if row.tenant_id != tenant_id or key not in wanted or row.paused is not None:
                continue
            if row.next_run_at is not None and (
                key not in earliest or row.next_run_at < earliest[key]
            ):
                earliest[key] = row.next_run_at
        return earliest

    async def put(
        self, schedule: Schedule, *, expected_version: int | None, create_only: bool
    ) -> tuple[Schedule, bool]:
        key = (schedule.tenant_id, schedule.schedule_id)
        current = self.rows.get(key)
        if current is not None and create_only:
            raise ScheduleIdTakenError("taken")
        if expected_version is not None and (
            current is None or current.version != expected_version
        ):
            raise HarborPreconditionFailedError("version mismatch")
        if current is None:
            self.rows[key] = schedule
            return schedule, True
        stored = replace(
            schedule,
            paused=current.paused,
            consecutive_failures=current.consecutive_failures,
            next_run_at=current.next_run_at,
            version=current.version + 1,
            created_by=current.created_by,
            created_at=current.created_at,
        )
        self.rows[key] = stored
        return stored, False

    async def set_paused(
        self, *, tenant_id: str, schedule_id: str, paused: SchedulePause | None, actor: str
    ) -> Schedule | None:
        current = self.rows.get((tenant_id, schedule_id))
        if current is None or (current.paused is None) == (paused is None):
            return current
        stored = replace(
            current,
            paused=paused,
            version=current.version + 1,
            updated_by=actor,
            consecutive_failures=current.consecutive_failures if paused else 0,
            next_run_at=None if paused else current.next_run_at,
        )
        self.rows[(tenant_id, schedule_id)] = stored
        return stored

    async def set_next_run_at(
        self, *, tenant_id: str, schedule_id: str, next_run_at: datetime | None
    ) -> None:
        current = self.rows.get((tenant_id, schedule_id))
        if current is not None:
            self.rows[(tenant_id, schedule_id)] = replace(current, next_run_at=next_run_at)

    async def record_run_outcome(self, *, tenant_id: str, schedule_id: str, failed: bool) -> int:
        current = self.rows.get((tenant_id, schedule_id))
        if current is None:
            return 0
        failures = current.consecutive_failures + 1 if failed else 0
        self.rows[(tenant_id, schedule_id)] = replace(current, consecutive_failures=failures)
        return failures

    async def claim_queue(self, *, tenant_id: str, schedule_id: str) -> bool:
        if (tenant_id, schedule_id) in self.waiting:
            return False
        self.waiting.add((tenant_id, schedule_id))
        return True

    async def release_queue(self, *, tenant_id: str, schedule_id: str) -> None:
        self.waiting.discard((tenant_id, schedule_id))

    async def record_slot(self, *, tenant_id: str, schedule_id: str, slot: ScheduleSlot) -> None:
        if (tenant_id, schedule_id) in self.rows:
            slots = self.slots.setdefault((tenant_id, schedule_id), [])
            slots.insert(0, slot)
            del slots[RECENT_SLOT_LIMIT:]

    async def recent_slots(
        self, *, tenant_id: str, schedule_ids: Sequence[str], limit: int
    ) -> dict[str, list[ScheduleSlot]]:
        return {
            schedule_id: self.slots[(tenant_id, schedule_id)][:limit]
            for schedule_id in schedule_ids
            if (tenant_id, schedule_id) in self.slots
        }

    async def delete(self, *, tenant_id: str, schedule_id: str) -> bool:
        self.slots.pop((tenant_id, schedule_id), None)
        return self.rows.pop((tenant_id, schedule_id), None) is not None


@dataclass
class FakeTimers:
    """Timer engine double that records every call and can be unreachable."""

    unreachable: bool = False
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def put(self, schedule: Schedule, *, catch_up_window_seconds: int) -> datetime | None:
        self._check()
        self.calls.append(("put", schedule.schedule_id, catch_up_window_seconds, schedule.paused))
        return None if schedule.paused is not None else NEXT_RUN

    async def pause(self, tenant_id: str, schedule_id: str, *, note: str) -> None:
        self._check()
        self.calls.append(("pause", schedule_id, note))

    async def next_run_at(self, tenant_id: str, schedule_id: str) -> datetime | None:
        self.calls.append(("next_run_at", schedule_id))
        return NEXT_RUN

    async def delete(self, tenant_id: str, schedule_id: str) -> None:
        self._check()
        self.calls.append(("delete", schedule_id))

    async def follow(self, tenant_id: str, schedule_id: str, ingestion_id: str) -> None:
        self._check()
        self.calls.append(("follow", schedule_id, ingestion_id))

    def _check(self) -> None:
        if self.unreachable:
            raise HarborUnavailableError("timer engine down")


@dataclass
class FakeStarter:
    """``IngestionStarter`` stand-in: queued runs ``ing_1``, ``ing_2``, ... or scripted errors."""

    errors: list[Exception] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def start(self, key: ConnectionKey, **options: Any) -> dict[str, Any]:
        self.calls.append({"key": key, **options})
        if self.errors:
            raise self.errors.pop(0)
        return {"ingestion_id": f"ing_{len(self.calls)}", "status": "queued"}


@dataclass
class FakeRuns:
    """Run registry stand-in holding tasks by ID."""

    tasks: dict[str, IngestionTask] = field(default_factory=dict)

    async def get(self, task_id: str) -> IngestionTask | None:
        return self.tasks.get(task_id)

    def add(self, task_id: str, status: IngestionTaskState, *, tenant_id: str = TENANT) -> None:
        self.tasks[task_id] = IngestionTask(
            task_id=task_id,
            source_scope_id="scope-1",
            status=status,
            request={"tenant_id": tenant_id},
            summary={"error_code": "connector_auth"},
        )


@dataclass
class FakeConfig:
    """``ConfigProvider`` stand-in answering the ``schedules.*`` settings."""

    values: dict[str, object]

    async def effective(self, tenant_id: str, level: SettingsLevel) -> EffectiveSettings:
        return EffectiveSettings(
            revision=1,
            values={
                f"schedules.{name}": EffectiveValue(value, "tenant")
                for name, value in self.values.items()
            },
        )
