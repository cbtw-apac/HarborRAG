"""ConnectionScheduleService through its interface, over in-memory stores and timers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from harborrag_core.base import utc_now
from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.resolution import EffectiveSettings, EffectiveValue
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborPreconditionFailedError,
    HarborUnavailableError,
    HarborValidationError,
    ScheduleIdTakenError,
    ScheduleNotFoundError,
    UnknownReferenceError,
)
from harborrag_core.domain.connection import Connection
from harborrag_core.domain.project import Project
from harborrag_core.domain.schedule import (
    RECENT_SLOT_LIMIT,
    Schedule,
    ScheduleFilter,
    SchedulePause,
    ScheduleSlot,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.testing.fakes import FakeSecrets
from harborrag_engine.control_plane.connection_views import ANY_VERSION
from harborrag_runtime.application.control_plane.connection_models import ConnectionKey
from harborrag_runtime.application.control_plane.schedule_models import (
    ScheduleKey,
    ScheduleQuery,
    ScheduleWrite,
    SlotAttempt,
    SlotStart,
    WriteConditions,
)
from harborrag_runtime.application.control_plane.schedules import ConnectionScheduleService
from harborrag_runtime.ingestion.connections import ConnectionUnavailableError
from harborrag_runtime.testing.connection_fakes import fake_control_plane

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "acme"
KEY = ScheduleKey(TENANT, "nightly")
WRITE = ScheduleWrite(project_id="demo", connection_id="demo-tickets", interval="PT1H")
NEXT_RUN = datetime(2026, 11, 28, 2, 0, tzinfo=UTC)
DUE = datetime(2026, 11, 27, 2, 0, tzinfo=UTC)
UNCONDITIONAL = WriteConditions()


class _Schedules:
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
class _Timers:
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

    async def resume(self, tenant_id: str, schedule_id: str) -> datetime | None:
        self._check()
        self.calls.append(("resume", schedule_id))
        return NEXT_RUN

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
class _Starter:
    """``IngestionStarter`` stand-in: queued runs ``ing_1``, ``ing_2``, ... or scripted errors."""

    errors: list[Exception] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def start(self, key: ConnectionKey, **options: Any) -> dict[str, Any]:
        self.calls.append({"key": key, **options})
        if self.errors:
            raise self.errors.pop(0)
        return {"ingestion_id": f"ing_{len(self.calls)}", "status": "queued"}


@dataclass
class _Runs:
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
class _Config:
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


@dataclass
class _World:
    service: ConnectionScheduleService
    schedules: _Schedules
    timers: _Timers
    starter: _Starter
    runs: _Runs


def _connection(connection_id: str = "demo-tickets", **changes: Any) -> Connection:
    return Connection(
        id=f"c-{connection_id}",
        tenant_id=TENANT,
        project_id="demo",
        connection_id=connection_id,
        source_key=f"demo/{connection_id}",
        source_type="jira",
        name="DEMO tickets",
        **changes,
    )


def _world(*, connection_status: str = "active", **settings: object) -> _World:
    control_plane = fake_control_plane(
        [Project(id="demo", name="DEMO", collection="demo", tenant_id=TENANT)], FakeSecrets()
    )
    control_plane.connections.connections.update(
        {
            "c-demo-tickets": _connection(status=connection_status),
            "c-demo-wiki": _connection("demo-wiki"),
        }
    )
    schedules, timers, starter, runs = _Schedules(), _Timers(), _Starter(), _Runs()
    config = (
        _Config({"enabled": True, "min_interval": "PT15M", "max_per_connection": 5, **settings})
        if settings
        else None
    )
    wired = replace(control_plane, schedules=schedules, config=config)

    async def provide_runs() -> _Runs:
        return runs

    async def provide_timers() -> _Timers:
        return timers

    service = ConnectionScheduleService(
        control_plane=lambda: wired,
        runs=provide_runs,
        starter=starter,
        timers=provide_timers,
    )
    return _World(service, schedules, timers, starter, runs)


async def _put(world: _World, write: ScheduleWrite = WRITE, key: ScheduleKey = KEY) -> Any:
    return await world.service.put(key, write, actor="u_ops", conditions=UNCONDITIONAL)


async def test_put_creates_a_schedule_and_sets_its_timer() -> None:
    world = _world()

    view, version, created = await _put(world)

    assert (created, version) == (True, 1)
    assert world.timers.calls == [("put", "nightly", 60, None)]
    assert view["interval"] == "PT1H" and view["cron"] is None
    assert view["state"] == "active" and view["next_run_at"] == NEXT_RUN
    assert view["input"] == {
        "project_id": "demo",
        "connection_id": "demo-tickets",
        "mode": "incremental",
    }


async def test_put_overwrite_keeps_the_pause_and_widens_the_catch_up_window() -> None:
    world = _world()
    await _put(world)
    await world.service.pause(KEY, reason="maintenance", actor="u_ops")

    view, version, created = await _put(world, replace(WRITE, interval="PT2H", catch_up=True))

    assert (created, version) == (False, 3)
    assert view["state"] == "paused" and view["paused"]["reason"] == "maintenance"
    assert view["interval"] == "PT2H" and view["next_run_at"] is None
    put_call = world.timers.calls[-1]
    assert put_call[:3] == ("put", "nightly", 7200) and put_call[3] is not None


async def test_put_removes_a_created_schedule_the_timer_could_not_follow() -> None:
    world = _world()
    world.timers.unreachable = True

    with pytest.raises(HarborUnavailableError):
        await _put(world)

    assert await world.schedules.get(tenant_id=TENANT, schedule_id="nightly") is None


async def test_put_keeps_an_overwritten_schedule_when_the_timer_is_unreachable() -> None:
    world = _world()
    await _put(world)
    world.timers.unreachable = True

    with pytest.raises(HarborUnavailableError):
        await _put(world, replace(WRITE, interval="PT2H"))

    stored = await world.schedules.get(tenant_id=TENANT, schedule_id="nightly")
    assert stored is not None and stored.interval_seconds == 7200


@pytest.mark.parametrize(
    ("changes", "fields"),
    [
        pytest.param({"interval": "PT5M"}, {"interval"}, id="under-min-interval"),
        pytest.param({"cron": "0 * * * *"}, {"interval"}, id="both-timetables"),
        pytest.param({"interval": None}, {"interval"}, id="no-timetable"),
        pytest.param({"interval": None, "cron": "*/5 * * * *"}, {"cron"}, id="cron-gap"),
        pytest.param({"timezone": "Mars/Olympus"}, {"timezone"}, id="bad-timezone"),
        pytest.param({"jitter": "PT31M"}, {"jitter"}, id="jitter-over-half"),
        pytest.param({"interval": "P1D", "jitter": "PT2H"}, {"jitter"}, id="jitter-over-hour"),
        pytest.param(
            {"interval": "PT5M", "jitter": "PT2H"}, {"interval", "jitter"}, id="every-issue"
        ),
    ],
)
async def test_put_refuses_a_timetable_that_breaks_a_rule(
    changes: dict[str, Any], fields: set[str]
) -> None:
    world = _world()

    with pytest.raises(HarborValidationError) as raised:
        await _put(world, replace(WRITE, **changes))

    errors = raised.value.details["errors"]
    assert isinstance(errors, list) and {error["field"] for error in errors} == fields
    assert world.timers.calls == [] and world.schedules.rows == {}


async def test_put_follows_the_tenant_min_interval() -> None:
    world = _world(min_interval="PT1M")

    _, _, created = await _put(world, replace(WRITE, interval="PT5M"))

    assert created is True


async def test_put_refuses_a_connection_past_max_per_connection() -> None:
    world = _world(max_per_connection=1)
    await _put(world)

    with pytest.raises(HarborValidationError) as raised:
        await _put(world, key=ScheduleKey(TENANT, "hourly"))
    _, version, created = await _put(world, replace(WRITE, interval="PT2H"))
    _, _, other_created = await _put(
        world, replace(WRITE, connection_id="demo-wiki"), key=ScheduleKey(TENANT, "hourly")
    )

    assert raised.value.details["errors"][0]["field"] == "input.connection_id"
    assert (version, created, other_created) == (2, False, True)


@pytest.mark.parametrize(
    ("changes", "field_name"),
    [
        ({"project_id": "missing"}, "input.project_id"),
        ({"connection_id": "missing"}, "input.connection_id"),
    ],
)
async def test_put_refuses_an_unknown_reference(changes: dict[str, str], field_name: str) -> None:
    world = _world()

    with pytest.raises(UnknownReferenceError) as raised:
        await _put(world, replace(WRITE, **changes))

    assert raised.value.details == {"field": field_name}


async def test_put_refuses_a_connection_being_deleted() -> None:
    world = _world(connection_status="deleting")

    with pytest.raises(ConnectionUnavailableError):
        await _put(world)


async def test_put_with_if_match_any_needs_an_existing_schedule() -> None:
    world = _world()

    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(
            KEY, WRITE, actor="u_ops", conditions=WriteConditions(expected_version=ANY_VERSION)
        )
    await _put(world)
    _, version, created = await world.service.put(
        KEY, WRITE, actor="u_ops", conditions=WriteConditions(expected_version=ANY_VERSION)
    )

    assert (version, created) == (2, False)


async def test_pause_and_resume_are_idempotent_and_drive_the_timer() -> None:
    world = _world()
    await _put(world)

    first = await world.service.pause(KEY, reason="maintenance", actor="u_ops")
    again = await world.service.pause(KEY, reason="other", actor="u_other")
    resumed = await world.service.resume(KEY, actor="u_ops")
    resumed_again = await world.service.resume(KEY, actor="u_ops")
    _, version = await world.service.get(KEY)

    assert first["paused"]["by"] == "u_ops" and first["next_run_at"] is None
    assert again["paused"] == first["paused"]
    assert resumed["state"] == "active" and resumed["next_run_at"] == NEXT_RUN
    assert resumed_again["state"] == "active" and version == 3
    assert [call for call in world.timers.calls if call[0] in {"pause", "resume"}] == [
        ("pause", "nightly", "maintenance"),
        ("pause", "nightly", "maintenance"),
        ("resume", "nightly"),
        ("resume", "nightly"),
    ]


async def test_pause_of_a_missing_schedule_is_not_found() -> None:
    world = _world()

    with pytest.raises(ScheduleNotFoundError):
        await world.service.pause(KEY, reason=None, actor="u_ops")
    with pytest.raises(ScheduleNotFoundError):
        await world.service.resume(KEY, actor="u_ops")


async def test_trigger_records_a_manual_slot_and_follows_the_run_while_paused() -> None:
    world = _world()
    await _put(world)
    await world.service.pause(KEY, reason=None, actor="u_ops")

    ingestion = await world.service.trigger(KEY)

    assert ingestion["ingestion_id"] == "ing_1"
    assert world.starter.calls[0]["actor"] == "nightly"
    assert world.starter.calls[0]["actor_type"] == "schedule"
    (slot,) = world.schedules.slots[(TENANT, "nightly")]
    assert (slot.outcome, slot.manual, slot.ingestion_id) == ("started", True, "ing_1")
    assert world.timers.calls[-1] == ("follow", "nightly", "ing_1")


async def test_trigger_passes_a_starter_error_through_without_a_slot() -> None:
    world = _world()
    await _put(world)
    world.starter.errors.append(
        HarborAlreadyRunningError("busy", details={"ingestion_id": "ing_busy"})
    )

    with pytest.raises(HarborAlreadyRunningError):
        await world.service.trigger(KEY)

    assert (TENANT, "nightly") not in world.schedules.slots


async def test_trigger_still_returns_the_run_when_it_cannot_be_followed() -> None:
    world = _world()
    await _put(world)
    world.timers.unreachable = True

    ingestion = await world.service.trigger(KEY)

    assert ingestion["ingestion_id"] == "ing_1"


async def test_run_slot_starts_the_schedule_operation() -> None:
    world = _world()
    await _put(world, replace(WRITE, mode="force"))

    start = await world.service.run_slot(KEY, due_at=DUE)

    assert start == SlotStart("started", "ing_1")
    assert world.starter.calls[0]["mode"] == "force"
    assert world.starter.calls[0]["key"] == ConnectionKey(TENANT, "demo", "demo-tickets")
    (slot,) = world.schedules.slots[(TENANT, "nightly")]
    assert (slot.due_at, slot.outcome, slot.manual) == (DUE, "started", False)


async def test_run_slot_skips_while_schedules_are_disabled() -> None:
    world = _world(enabled=False)
    await _put(world)

    start = await world.service.run_slot(KEY, due_at=DUE)

    assert start == SlotStart("skipped")
    assert world.starter.calls == []
    assert world.schedules.slots[(TENANT, "nightly")][0].reason == "disabled_by_setting"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ConnectionUnavailableError("deleting"), "connection_deleting"),
        (UnknownReferenceError("gone", {"field": "connection_id"}), "reference_not_found"),
    ],
)
async def test_run_slot_records_why_a_run_could_not_start(error: Exception, reason: str) -> None:
    world = _world()
    await _put(world)
    world.starter.errors.append(error)

    start = await world.service.run_slot(KEY, due_at=DUE)

    assert start.outcome == "skipped"
    assert world.schedules.slots[(TENANT, "nightly")][0].reason == reason


async def test_run_slot_skips_a_busy_connection_by_default() -> None:
    world = _world()
    await _put(world)
    world.starter.errors.append(
        HarborAlreadyRunningError("busy", details={"ingestion_id": "ing_busy"})
    )

    start = await world.service.run_slot(KEY, due_at=DUE)

    assert start == SlotStart("skipped", "ing_busy")
    slot = world.schedules.slots[(TENANT, "nightly")][0]
    assert (slot.outcome, slot.reason, slot.ingestion_id) == (
        "skipped",
        "already_running",
        "ing_busy",
    )


async def test_run_slot_queues_one_slot_and_requeues_it_without_a_new_record() -> None:
    world = _world()
    await _put(world, replace(WRITE, if_running="queue"))
    busy = HarborAlreadyRunningError("busy", details={"ingestion_id": "ing_busy"})
    world.starter.errors.extend([busy, busy, busy])

    queued = await world.service.run_slot(KEY, due_at=DUE)
    second = await world.service.run_slot(KEY, due_at=DUE)
    requeued = await world.service.run_slot(KEY, due_at=DUE, attempt=SlotAttempt.AFTER_WAIT)
    started = await world.service.run_slot(KEY, due_at=DUE, attempt=SlotAttempt.AFTER_WAIT)

    assert queued == SlotStart("queued", "ing_busy") and requeued == SlotStart("queued", "ing_busy")
    assert second == SlotStart("skipped", "ing_busy")
    assert started.outcome == "started"
    assert [slot.outcome for slot in world.schedules.slots[(TENANT, "nightly")]] == [
        "started",
        "skipped",
        "queued",
    ]
    assert world.schedules.waiting == set()


async def test_run_slot_gives_the_waiting_place_back_when_its_slot_cannot_be_recorded() -> None:
    world = _world()
    await _put(world, replace(WRITE, if_running="queue"))
    world.starter.errors.append(
        HarborAlreadyRunningError("busy", details={"ingestion_id": "ing_busy"})
    )

    async def unrecordable(**_: Any) -> None:
        raise HarborUnavailableError("control database down")

    world.schedules.record_slot = unrecordable  # type: ignore[method-assign]  # test double

    with pytest.raises(HarborUnavailableError):
        await world.service.run_slot(KEY, due_at=DUE)

    assert world.schedules.waiting == set()


async def test_put_sets_the_timer_again_when_another_write_moved_the_version() -> None:
    world = _world()
    await _put(world)
    timed: list[int | None] = []
    put_timer = world.timers.put

    async def racing_put(schedule: Schedule, *, catch_up_window_seconds: int) -> datetime | None:
        if not timed:
            stored = world.schedules.rows[(TENANT, "nightly")]
            world.schedules.rows[(TENANT, "nightly")] = replace(
                stored, version=stored.version + 1, interval_seconds=7_200
            )
        timed.append(schedule.interval_seconds)
        return await put_timer(schedule, catch_up_window_seconds=catch_up_window_seconds)

    world.timers.put = racing_put  # type: ignore[method-assign]  # test double

    await _put(world, replace(WRITE, interval="PT3H"))

    assert timed == [10_800, 7_200]


@pytest.mark.parametrize("paused", [True, False])
async def test_run_slot_ignores_a_paused_or_missing_schedule(paused: bool) -> None:
    world = _world()
    if paused:
        await _put(world)
        await world.service.pause(KEY, reason=None, actor="u_ops")

    start = await world.service.run_slot(KEY, due_at=DUE)

    assert start == SlotStart("ignored")
    assert world.starter.calls == []


async def test_finish_run_pauses_as_system_after_the_failure_limit() -> None:
    world = _world()
    await _put(world, replace(WRITE, pause_after_failures=2))
    world.runs.add("ing_1", IngestionTaskState.FAILED)
    world.runs.add("ing_2", IngestionTaskState.FAILED)

    await world.service.finish_run(KEY, "ing_1")
    view, _ = await world.service.get(KEY)
    await world.service.finish_run(KEY, "ing_2")
    paused, _ = await world.service.get(KEY)

    assert view["consecutive_failures"] == 1 and view["state"] == "active"
    assert paused["state"] == "paused" and paused["paused"]["by"] == "system"
    reason = "Paused after 2 failed runs in a row (connector_auth)"
    assert paused["paused"]["reason"] == reason
    assert world.timers.calls[-1] == ("pause", "nightly", reason)


async def test_finish_run_resets_on_success_and_ignores_cancelled_and_foreign_runs() -> None:
    world = _world()
    await _put(world)
    world.runs.add("ing_failed", IngestionTaskState.FAILED)
    world.runs.add("ing_cancelled", IngestionTaskState.CANCELLED)
    world.runs.add("ing_foreign", IngestionTaskState.FAILED, tenant_id="other")
    world.runs.add("ing_ok", IngestionTaskState.PARTIAL)

    await world.service.finish_run(KEY, "ing_failed")
    await world.service.finish_run(KEY, "ing_cancelled")
    await world.service.finish_run(KEY, "ing_foreign")
    await world.service.finish_run(KEY, "ing_missing")
    counted, _ = await world.service.get(KEY)
    await world.service.finish_run(KEY, "ing_ok")
    reset, _ = await world.service.get(KEY)

    assert counted["consecutive_failures"] == 1
    assert reset["consecutive_failures"] == 0


async def test_run_ended_reads_the_run_registry() -> None:
    world = _world()
    world.runs.add("ing_running", IngestionTaskState.RUNNING)
    world.runs.add("ing_failed", IngestionTaskState.FAILED)
    world.runs.add("ing_foreign", IngestionTaskState.RUNNING, tenant_id="other")

    assert await world.service.run_ended(KEY, "ing_running") is False
    assert await world.service.run_ended(KEY, "ing_failed") is True
    assert await world.service.run_ended(KEY, "ing_missing") is True
    assert await world.service.run_ended(KEY, "ing_foreign") is True


async def test_remove_for_connection_removes_only_that_connections_schedules() -> None:
    world = _world()
    await _put(world)
    await _put(world, replace(WRITE, interval="PT2H"), key=ScheduleKey(TENANT, "hourly"))
    await _put(world, replace(WRITE, connection_id="demo-wiki"), key=ScheduleKey(TENANT, "wiki"))
    connection = ConnectionKey(TENANT, "demo", "demo-tickets")

    await world.service.remove_for_connection(connection)
    await world.service.remove_for_connection(connection)

    assert set(world.schedules.rows) == {(TENANT, "wiki")}
    deleted = [call[1] for call in world.timers.calls if call[0] == "delete"]
    assert sorted(deleted) == ["hourly", "nightly"]


async def test_delete_removes_the_timer_then_the_schedule() -> None:
    world = _world()
    await _put(world)

    await world.service.delete(KEY)

    assert world.timers.calls[-1] == ("delete", "nightly")
    assert world.schedules.rows == {}
    with pytest.raises(ScheduleNotFoundError):
        await world.service.delete(KEY)


async def test_views_show_slot_runs_and_hide_next_run_while_disabled() -> None:
    world = _world(enabled=False)
    await _put(world)
    world.schedules.slots[(TENANT, "nightly")] = [
        ScheduleSlot(due_at=utc_now(), outcome="started", ingestion_id="ing_1"),
        ScheduleSlot(due_at=DUE, outcome="started", ingestion_id="ing_foreign"),
    ]
    world.runs.add("ing_1", IngestionTaskState.RUNNING)
    world.runs.add("ing_foreign", IngestionTaskState.RUNNING, tenant_id="other")

    view, _ = await world.service.get(KEY)
    page = await world.service.page(TENANT, ScheduleQuery(filters=ScheduleFilter(), limit=10))

    assert view["next_run_at"] is None
    assert view["recent_slots"][0]["run"] == {
        "type": "ingestion",
        "id": "ing_1",
        "status": "running",
    }
    assert view["recent_slots"][1]["run"] is None
    assert [len(item["recent_slots"]) for item in page["items"]] == [1]
    assert page["next_cursor"] is None
