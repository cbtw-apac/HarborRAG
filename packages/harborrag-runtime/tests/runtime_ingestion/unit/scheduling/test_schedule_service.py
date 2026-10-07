"""Schedule service rules for keeping live pause state across updates."""

from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_runtime.scheduling import ScheduleService
from harborrag_runtime.scheduling.config import ScheduleCatalog
from harborrag_runtime.scheduling.errors import DuplicateScheduleError, ScheduleNotFoundError
from harborrag_runtime.scheduling.models import (
    ScheduleDefinition,
    ScheduledWorkflow,
    ScheduleOwner,
    ScheduleView,
    SourceScheduleTarget,
)


class _Backend:
    """In-memory backend whose state, like Temporal's, is separate from the definition."""

    def __init__(self) -> None:
        self.definitions: dict[str, ScheduleDefinition] = {}
        self.paused: dict[str, bool] = {}
        self.notes: dict[str, str | None] = {}
        self.calls: list[tuple[str, str]] = []

    async def create(self, definition, source) -> None:
        if definition.schedule_id in self.definitions:
            raise DuplicateScheduleError(definition.schedule_id)
        self.definitions[definition.schedule_id] = definition
        self.paused[definition.schedule_id] = definition.paused
        self.notes[definition.schedule_id] = definition.note

    async def update(self, definition, source) -> None:
        self.calls.append((definition.schedule_id, "update"))
        self.definitions[definition.schedule_id] = definition

    async def describe(self, schedule_id: str) -> ScheduleView:
        if schedule_id not in self.definitions:
            raise ScheduleNotFoundError(schedule_id)
        stored = self.definitions[schedule_id]
        return ScheduleView(
            definition=replace(
                stored, paused=self.paused[schedule_id], note=self.notes[schedule_id]
            ),
            desired_paused=stored.paused,
        )

    async def list_all(self) -> list[ScheduleView]:
        return [await self.describe(schedule_id) for schedule_id in self.definitions]

    async def pause(self, schedule_id: str, *, note: str | None) -> None:
        self.calls.append((schedule_id, "pause"))
        self.paused[schedule_id] = True
        self.notes[schedule_id] = note

    async def unpause(self, schedule_id: str, *, note: str | None) -> None:
        self.calls.append((schedule_id, "unpause"))
        self.paused[schedule_id] = False
        self.notes[schedule_id] = note

    async def trigger(self, schedule_id: str) -> None: ...

    async def delete(self, schedule_id: str) -> None: ...

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
    return ScheduleService(backend, lambda submission: object())  # type: ignore[arg-type,return-value]


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
    assert view.desired_paused is False


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
