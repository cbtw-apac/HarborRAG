"""sync_ingestion_progress: the diff loop behind the SSE live tail (ML2).

This is the piece api/v1/ingestion routes.py's stream route depends on for
anything beyond backlog replay -- a fake PublicTaskStore proves progress
diffs get published+persisted, and that a subscriber opened before a tick
receives the resulting events live (not just after reconnecting).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from harborrag_app.workflow_control.ingestion.progress_bridge import sync_ingestion_progress
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.events.in_process import InProcessEventBus


@dataclass
class _FakeTask:
    task_id: str
    status: IngestionTaskState
    summary: dict[str, object] = field(default_factory=dict)
    submitted_at: object = None


class FakeTaskStore:
    """Just enough of PublicTaskStore for the progress bridge to drive."""

    def __init__(
        self,
        tasks: list[_FakeTask],
        counts: dict[str, dict[str, int]],
        *,
        finish_on_progress: dict[str, IngestionTaskState] | None = None,
    ) -> None:
        self._tasks = {task.task_id: task for task in tasks}
        self._counts = counts
        # Simulates a worker finishing the task in the window between this
        # tick's list_active() and its own re-check of current status --
        # exactly the race sync_ingestion_progress's re-fetch is there to catch.
        self._finish_on_progress = dict(finish_on_progress or {})
        self.appended: list[tuple[str, HarborEvent]] = []
        self._next_seq: dict[str, int] = {}

    async def list_active(
        self,
        *,
        after_submitted_at: object | None = None,
        after_task_id: str | None = None,
        limit: int = 500,
    ) -> tuple[_FakeTask, ...]:
        del after_submitted_at, after_task_id, limit
        return tuple(
            task
            for task in self._tasks.values()
            if task.status in (IngestionTaskState.PENDING, IngestionTaskState.RUNNING)
        )

    async def get(self, task_id: str) -> _FakeTask | None:
        return self._tasks.get(task_id)

    async def progress(self, task_id: str) -> dict[str, int]:
        if task_id in self._finish_on_progress:
            self._tasks[task_id].status = self._finish_on_progress.pop(task_id)
        return self._counts.get(task_id, {})

    async def update_summary(self, task_id: str, values: dict[str, object]) -> None:
        self._tasks[task_id].summary.update(values)

    async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent:
        seq = self._next_seq.get(task_id, 0) + 1
        self._next_seq[task_id] = seq
        stamped = replace(event, seq=seq)
        self.appended.append((task_id, stamped))
        return stamped


@pytest.mark.asyncio
async def test_progress_change_is_published_and_appended_to_the_task_event_log() -> None:
    store = FakeTaskStore(
        [_FakeTask("t1", IngestionTaskState.RUNNING)],
        {"t1": {"succeeded": 1}},
    )
    bus = InProcessEventBus()
    subscriber = bus.subscribe("task.t1.")

    examined = await sync_ingestion_progress(store, bus)

    assert examined == 1
    event = await subscriber.__anext__()
    assert event.name == "task.t1.progress"
    assert event.payload["counts"] == {"succeeded": 1}
    assert store.appended == [("t1", event)]


@pytest.mark.asyncio
async def test_unchanged_progress_publishes_nothing_on_the_next_tick() -> None:
    store = FakeTaskStore(
        [_FakeTask("t1", IngestionTaskState.RUNNING)],
        {"t1": {"succeeded": 1}},
    )
    bus = InProcessEventBus()
    await sync_ingestion_progress(store, bus)  # first tick: publishes + records the snapshot

    subscriber = bus.subscribe("task.t1.")
    await sync_ingestion_progress(store, bus)  # second tick: same counts, nothing new

    with pytest.raises(TimeoutError):
        import asyncio

        await asyncio.wait_for(subscriber.__anext__(), timeout=0.05)


@dataclass
class _PagingFakeTaskStore:
    """Unlike FakeTaskStore, actually respects ``limit``/the cursor -- needed
    to exercise sync_ingestion_progress's multi-page loop, where FakeTaskStore
    (which always returns every task in one call) never triggers a second
    list_active() call at all."""

    tasks: list[_FakeTask]
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    async def list_active(
        self,
        *,
        after_submitted_at: object | None = None,
        after_task_id: str | None = None,
        limit: int = 500,
    ) -> tuple[_FakeTask, ...]:
        del after_submitted_at
        ordered = sorted(self.tasks, key=lambda task: task.task_id)
        if after_task_id is not None:
            ordered = [task for task in ordered if task.task_id > after_task_id]
        return tuple(ordered[:limit])

    async def get(self, task_id: str) -> _FakeTask | None:
        return next((task for task in self.tasks if task.task_id == task_id), None)

    async def progress(self, task_id: str) -> dict[str, int]:
        return self.counts.get(task_id, {})

    async def update_summary(self, task_id: str, values: dict[str, object]) -> None:
        next(task for task in self.tasks if task.task_id == task_id).summary.update(values)

    async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent:
        del task_id
        return event


@pytest.mark.asyncio
async def test_losing_the_lease_mid_tick_stops_further_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """still_leader is re-checked before every page. A tick spanning enough
    active tasks to outlive the lease's ttl must stop as soon as ownership
    is no longer confirmed, rather than keep writing progress/done events
    for later pages alongside whatever process took the lease over next."""
    monkeypatch.setattr(
        "harborrag_app.workflow_control.ingestion.progress_bridge._LIST_ACTIVE_PAGE_LIMIT", 1
    )
    store = _PagingFakeTaskStore(
        tasks=[
            _FakeTask("t1", IngestionTaskState.RUNNING),
            _FakeTask("t2", IngestionTaskState.RUNNING),
        ],
        counts={"t1": {"succeeded": 1}, "t2": {"succeeded": 1}},
    )
    bus = InProcessEventBus()
    leader_calls = 0

    async def still_leader() -> bool:
        nonlocal leader_calls
        leader_calls += 1
        return leader_calls == 1  # loses the lease right after the first page

    examined = await sync_ingestion_progress(store, bus, still_leader=still_leader)

    assert examined == 1  # only t1's page was processed
    assert leader_calls == 2  # checked before page 1, then again before the (aborted) page 2


@pytest.mark.asyncio
async def test_reaching_a_terminal_state_publishes_a_done_event_live() -> None:
    task = _FakeTask("t1", IngestionTaskState.RUNNING)
    # The task is RUNNING (so list_active() picks it up this tick) but finishes
    # by the time progress() is queried -- the race the re-fetch-after-progress
    # in sync_ingestion_progress exists to catch within the same tick.
    store = FakeTaskStore(
        [task],
        {"t1": {"succeeded": 2}},
        finish_on_progress={"t1": IngestionTaskState.COMPLETED},
    )
    bus = InProcessEventBus()
    subscriber = bus.subscribe("task.t1.")

    await sync_ingestion_progress(store, bus)

    progress_event = await subscriber.__anext__()
    assert progress_event.name == "task.t1.progress"
    done_event = await subscriber.__anext__()
    assert done_event.name == "task.t1.done"
    assert done_event.payload["status"] == "COMPLETED"
