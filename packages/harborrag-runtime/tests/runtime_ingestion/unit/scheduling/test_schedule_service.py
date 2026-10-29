"""Schedule service rules for pause state, ownership and config reconciliation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_runtime.ingestion.connections import ConnectionNotFoundError
from harborrag_runtime.scheduling import ScheduleService
from harborrag_runtime.scheduling.config import ScheduleCatalog
from harborrag_runtime.scheduling.errors import (
    DuplicateScheduleError,
    ScheduleNotFoundError,
    ScheduleOwnershipError,
    ScheduleValidationError,
)
from harborrag_runtime.scheduling.models import (
    ScheduleDefinition,
    ScheduledWorkflow,
    ScheduleOverlap,
    ScheduleOwner,
    ScheduleView,
    SourceScheduleTarget,
)


class _Backend:
    """In-memory backend that, like Temporal, keeps live state apart from the action memo."""

    def __init__(self) -> None:
        self.memos: dict[str, dict[str, object]] = {}
        self.foreign: set[str] = set()
        self.rejected: set[str] = set()
        self.paused: dict[str, bool] = {}
        self.notes: dict[str, str | None] = {}
        self.calls: list[tuple[str, str]] = []

    async def create(self, definition: ScheduleDefinition) -> None:
        schedule_id = definition.schedule_id
        if schedule_id in self.rejected:
            raise ScheduleValidationError(f"Temporal rejected schedule {schedule_id!r}")
        if schedule_id in self.memos or schedule_id in self.foreign:
            raise DuplicateScheduleError(schedule_id)
        self.memos[schedule_id] = definition.to_memo()
        self.paused[schedule_id] = definition.paused
        self.notes[schedule_id] = definition.note

    async def update(self, definition: ScheduleDefinition, *, desired_paused: bool) -> None:
        self.calls.append((definition.schedule_id, "update"))
        self.memos[definition.schedule_id] = definition.to_memo(desired_paused=desired_paused)

    async def describe(self, schedule_id: str) -> ScheduleView:
        if schedule_id not in self.memos:
            raise ScheduleNotFoundError(schedule_id)
        memo = self.memos[schedule_id]
        return ScheduleView(
            definition=ScheduleDefinition.from_memo(
                memo, paused=self.paused[schedule_id], note=self.notes[schedule_id]
            ),
            desired_paused=bool(memo["desired_paused"]),
            declared_paused=(bool(memo["declared_paused"]) if "declared_paused" in memo else None),
        )

    async def list_all(self) -> list[ScheduleView]:
        return [await self.describe(schedule_id) for schedule_id in self.memos]

    async def pause(self, schedule_id: str, *, note: str | None) -> None:
        self.calls.append((schedule_id, "pause"))
        self.memos[schedule_id] = {**self.memos[schedule_id], "desired_paused": True}
        self.paused[schedule_id] = True
        self.notes[schedule_id] = note

    async def unpause(self, schedule_id: str, *, note: str | None) -> None:
        self.calls.append((schedule_id, "unpause"))
        self.memos[schedule_id] = {**self.memos[schedule_id], "desired_paused": False}
        self.paused[schedule_id] = False
        self.notes[schedule_id] = note

    async def trigger(self, schedule_id: str) -> None:
        self.calls.append((schedule_id, "trigger"))

    async def delete(self, schedule_id: str) -> None:
        self.calls.append((schedule_id, "delete"))
        del self.memos[schedule_id]

    async def backfill(self, schedule_id, *, start_at, end_at) -> None: ...


def _definition(**overrides) -> ScheduleDefinition:
    values = {
        "schedule_id": "daily-sync",
        "workflow": ScheduledWorkflow.SOURCE_INGESTION,
        "cron": "0 1 * * *",
        "target": SourceScheduleTarget(tenant_id="DEFAULT", connection_id="workspace"),
    }
    values.update(overrides)
    return ScheduleDefinition(**values)


def _service(backend: _Backend) -> ScheduleService:
    return ScheduleService(backend, _prepared)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_config_resync_keeps_an_operator_pause() -> None:
    backend = _Backend()
    service = _service(backend)
    declared = _definition(owner=ScheduleOwner.CONFIG)
    await service.sync(ScheduleCatalog(schedules=(declared,)))
    await service.pause_schedule("daily-sync", note="connector outage")

    report = await service.sync(ScheduleCatalog(schedules=(replace(declared, cron="0 2 * * *"),)))

    view = await service.describe_schedule("daily-sync")
    assert report.updated == ("daily-sync",)
    assert view.definition.cron == "0 2 * * *"
    assert view.definition.paused is True
    assert view.definition.note == "connector outage"
    assert view.desired_paused is True


@pytest.mark.asyncio
async def test_update_without_applying_pause_leaves_state_alone() -> None:
    backend = _Backend()
    service = _service(backend)
    await service.create_schedule(_definition())
    await service.pause_schedule("daily-sync", note="incident")

    view = await service.update_schedule(_definition(cron="0 3 * * *"), apply_pause=False)

    assert view.definition.paused is True
    assert view.definition.note == "incident"
    assert ("daily-sync", "unpause") not in backend.calls


@pytest.mark.asyncio
async def test_update_applies_an_explicit_pause_change() -> None:
    backend = _Backend()
    service = _service(backend)
    await service.create_schedule(_definition())

    view = await service.update_schedule(_definition(paused=True, note="maintenance"))

    assert view.definition.paused is True
    assert view.definition.note == "maintenance"
    assert backend.calls == [("daily-sync", "update"), ("daily-sync", "pause")]


@pytest.mark.asyncio
async def test_update_skips_pause_calls_when_state_already_matches() -> None:
    backend = _Backend()
    service = _service(backend)
    await service.create_schedule(_definition())

    await service.update_schedule(_definition(paused=False))

    assert backend.calls == [("daily-sync", "update")]


@pytest.mark.asyncio
async def test_api_pause_and_unpause_record_the_desired_state() -> None:
    backend = _Backend()
    service = _service(backend)
    await service.create_schedule(_definition())

    await service.pause_schedule("daily-sync", note="incident")
    paused = await service.describe_schedule("daily-sync")
    await service.unpause_schedule("daily-sync")
    resumed = await service.describe_schedule("daily-sync")

    assert (paused.definition.paused, paused.desired_paused) == (True, True)
    assert (resumed.definition.paused, resumed.desired_paused) == (False, False)


@pytest.mark.asyncio
async def test_update_without_applying_pause_keeps_the_desired_state() -> None:
    backend = _Backend()
    service = _service(backend)
    await service.create_schedule(_definition())
    await service.pause_schedule("daily-sync", note="incident")

    view = await service.update_schedule(_definition(cron="0 3 * * *"), apply_pause=False)

    assert view.desired_paused is True


@pytest.mark.asyncio
async def test_config_resync_applies_a_changed_declared_pause() -> None:
    backend = _Backend()
    service = _service(backend)
    declared = _definition(owner=ScheduleOwner.CONFIG)
    await service.sync(ScheduleCatalog(schedules=(declared,)))

    await service.sync(ScheduleCatalog(schedules=(replace(declared, paused=True),)))
    paused = await service.describe_schedule("daily-sync")
    await service.sync(ScheduleCatalog(schedules=(replace(declared, paused=False),)))
    resumed = await service.describe_schedule("daily-sync")

    assert (paused.definition.paused, paused.desired_paused) == (True, True)
    assert (resumed.definition.paused, resumed.desired_paused) == (False, False)


@pytest.mark.asyncio
async def test_config_resync_ignores_pause_when_the_declaration_is_unchanged() -> None:
    backend = _Backend()
    service = _service(backend)
    declared = _definition(owner=ScheduleOwner.CONFIG)
    await service.sync(ScheduleCatalog(schedules=(declared,)))
    await service.pause_schedule("daily-sync", note="incident")

    await service.sync(ScheduleCatalog(schedules=(declared,)))

    assert (await service.describe_schedule("daily-sync")).definition.paused is True
    assert ("daily-sync", "unpause") not in backend.calls


@pytest.mark.asyncio
async def test_sync_reports_conflicts_and_rejections_and_keeps_going() -> None:
    backend = _Backend()
    backend.foreign.add("someone-elses")
    backend.rejected.add("rejected")
    service = _service(backend)
    await service.create_schedule(_definition(schedule_id="api-owned"))
    catalog = ScheduleCatalog(
        schedules=tuple(
            _definition(schedule_id=schedule_id, owner=ScheduleOwner.CONFIG)
            for schedule_id in ("someone-elses", "api-owned", "rejected", "daily-sync")
        )
    )

    report = await service.sync(catalog)

    assert report.failed == ("someone-elses", "api-owned", "rejected")
    assert report.created == ("daily-sync",)


@pytest.mark.asyncio
async def test_prune_deletes_only_undeclared_config_schedules() -> None:
    backend = _Backend()
    service = _service(backend)
    await service.create_schedule(_definition(schedule_id="api-owned"))
    await service.sync(
        ScheduleCatalog(
            schedules=(
                _definition(schedule_id="kept", owner=ScheduleOwner.CONFIG),
                _definition(schedule_id="removed", owner=ScheduleOwner.CONFIG),
            )
        )
    )

    report = await service.sync(
        ScheduleCatalog(
            schedules=(_definition(schedule_id="kept", owner=ScheduleOwner.CONFIG),),
            prune=True,
        )
    )

    assert report.deleted == ("removed",)
    assert sorted(backend.memos) == ["api-owned", "kept"]


@pytest.mark.asyncio
async def test_api_cannot_delete_a_config_schedule() -> None:
    backend = _Backend()
    service = _service(backend)
    await service.sync(ScheduleCatalog(schedules=(_definition(owner=ScheduleOwner.CONFIG),)))

    with pytest.raises(ScheduleOwnershipError):
        await service.delete_schedule("daily-sync")


@pytest.mark.asyncio
async def test_actions_reject_schedules_harborrag_does_not_own() -> None:
    backend = _Backend()
    backend.foreign.add("someone-elses")
    service = _service(backend)

    with pytest.raises(ScheduleNotFoundError):
        await service.pause_schedule("someone-elses")
    with pytest.raises(ScheduleNotFoundError):
        await service.trigger_now("someone-elses")
    assert backend.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overlap",
    [ScheduleOverlap.ALLOW_ALL, ScheduleOverlap.CANCEL_OTHER, ScheduleOverlap.TERMINATE_OTHER],
)
async def test_concurrent_and_cancelling_overlaps_are_rejected(overlap: ScheduleOverlap) -> None:
    service = _service(_Backend())

    with pytest.raises(ScheduleValidationError, match="not allowed"):
        await service.create_schedule(_definition(overlap=overlap))


@pytest.mark.asyncio
async def test_create_rejects_a_connector_that_does_not_resolve() -> None:
    async def prepare(submission):
        raise ConnectionNotFoundError(f"connection {submission.connector_name!r} does not exist")

    service = ScheduleService(_Backend(), prepare)

    with pytest.raises(ScheduleValidationError, match="does not exist"):
        await service.create_schedule(_definition())


async def _prepared(_submission: object) -> object:
    return object()
