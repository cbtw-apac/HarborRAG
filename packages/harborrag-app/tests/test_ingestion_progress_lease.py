"""AppService.sync_ingestion_progress must not double-tick across processes.

Regression coverage for a review finding: with the API scaled to more than
one process/replica, every process's background loop called the progress
bridge unconditionally, so N processes could each observe the same task's
changed counts and each append+publish a "progress" event for it -- distinct
rows (atomic seq), but duplicates all the same. The fix gates the tick
behind a DB-backed lease; this proves two AppService instances sharing one
lease repository (i.e. two processes against the same database) never both
tick in the same round.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.testing.control_plane_fakes import (
    FakeActivityRepository,
    FakeJobRepository,
    FakeLeaseRepository,
    FakeMemberRepository,
    FakePendingEffectRepository,
    FakeProjectRepository,
    FakeProviderRepository,
    FakeSettingsRepository,
    FakeSourceRepository,
)
from harborrag_core.testing.fakes import FakeSecrets
from harborrag_runtime.agent import InMemoryAgentRunRepository
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.events.in_process import InProcessEventBus
from harborrag_runtime.memory import InMemoryConversationMemory


@dataclass
class _FakeTask:
    task_id: str
    status: IngestionTaskState
    summary: dict[str, object] = field(default_factory=dict)


class _FakeTaskStore:
    """Just enough of PublicTaskStore to drive one progress-bridge tick."""

    def __init__(self, task: _FakeTask, counts: dict[str, int]) -> None:
        self._task = task
        self._counts = counts
        self.tick_count = 0

    async def list_active(self, *, limit: int = 500) -> tuple[_FakeTask, ...]:
        del limit
        return (self._task,)

    async def progress(self, task_id: str) -> dict[str, int]:
        del task_id
        self.tick_count += 1
        return self._counts

    async def get(self, task_id: str) -> _FakeTask | None:
        return self._task if task_id == self._task.task_id else None

    async def update_summary(self, task_id: str, values: dict[str, object]) -> None:
        del task_id
        self._task.summary.update(values)

    async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent:
        del task_id
        return event


def _build_service(
    *, lease_repository: FakeLeaseRepository, task_store: _FakeTaskStore, bus: InProcessEventBus
) -> AppService:
    control_plane = ControlPlaneRepositories(
        projects=FakeProjectRepository(),
        sources=FakeSourceRepository(),
        jobs=FakeJobRepository(),
        activity=FakeActivityRepository(),
        settings=FakeSettingsRepository(WorkspaceSettings(tenant_id="DEFAULT")),
        providers=FakeProviderRepository(),
        members=FakeMemberRepository(),
        conversation_memory=InMemoryConversationMemory(),
        agent_runs=InMemoryAgentRunRepository(),
        secrets=FakeSecrets(),
        pending_effects=FakePendingEffectRepository(),
        leases=lease_repository,
    )
    composition = CompositionRoot(
        control_plane=control_plane,
        control_db={"ping": "ok", "scheme": "test"},
        mode="test",
    )

    async def registry_factory(_settings):
        return task_store

    return AppService(
        composition,
        factories=AppServiceFactories(
            task_registry=registry_factory,
            event_bus=lambda: bus,
        ),
    )


@pytest.mark.asyncio
async def test_only_the_lease_holder_ticks_when_two_processes_share_a_database() -> None:
    """Two AppService instances (simulating two processes) share one lease
    repository, task store, and event bus -- the shape of the same database
    seen by two API processes. Only one of them should ever tick."""

    lease_repository = FakeLeaseRepository()
    task = _FakeTask("t1", IngestionTaskState.RUNNING)
    task_store = _FakeTaskStore(task, {"succeeded": 1})
    bus = InProcessEventBus()
    process_a = _build_service(lease_repository=lease_repository, task_store=task_store, bus=bus)
    process_b = _build_service(lease_repository=lease_repository, task_store=task_store, bus=bus)

    examined_a = await process_a.sync_ingestion_progress()
    examined_b = await process_b.sync_ingestion_progress()

    assert (examined_a, examined_b) == (1, 0)
    assert task_store.tick_count == 1  # the non-leader never touched the store at all


@pytest.mark.asyncio
async def test_the_lease_fails_over_once_the_holder_stops_renewing() -> None:
    """A process that stops ticking (crash, restart) must not permanently
    starve every other instance of the lease."""

    lease_repository = FakeLeaseRepository()
    task = _FakeTask("t1", IngestionTaskState.RUNNING)
    task_store = _FakeTaskStore(task, {"succeeded": 1})
    bus = InProcessEventBus()
    process_a = _build_service(lease_repository=lease_repository, task_store=task_store, bus=bus)
    process_b = _build_service(lease_repository=lease_repository, task_store=task_store, bus=bus)

    assert await process_a.sync_ingestion_progress() == 1  # a becomes leader
    # a's lease lapses (crash / restart / a machine clock rolling forward past ttl):
    lease_repository.leases.clear()

    assert await process_b.sync_ingestion_progress() == 1  # b takes over
