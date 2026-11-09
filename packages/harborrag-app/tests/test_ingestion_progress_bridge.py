"""sync_ingestion_progress: the diff loop behind the SSE live tail (ML2).

This is the piece api/v1/ingestion routes.py's stream route depends on for
anything beyond backlog replay -- a fake PublicTaskStore proves progress
diffs get published+persisted, and that a subscriber opened before a tick
receives the resulting events live (not just after reconnecting).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from harborrag_app.workflow_control.ingestion.progress_bridge import (
    sync_ingestion_progress,
    sync_run,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTaskState, StoredTaskDocumentResult, TaskDocumentPage
from harborrag_runtime.events.in_process import InProcessEventBus


@dataclass
class _FakeTask:
    task_id: str
    status: IngestionTaskState
    summary: dict[str, object] = field(default_factory=dict)
    request: dict[str, object] = field(default_factory=dict)
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
        self.failed_documents: dict[str, list[StoredTaskDocumentResult]] = {}
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
            if task.status
            in (
                IngestionTaskState.PENDING,
                IngestionTaskState.RUNNING,
                IngestionTaskState.PAUSED,
            )
        )

    async def get(self, task_id: str) -> _FakeTask | None:
        return self._tasks.get(task_id)

    async def progress(self, task_id: str) -> dict[str, int]:
        if task_id in self._finish_on_progress:
            self._tasks[task_id].status = self._finish_on_progress.pop(task_id)
        return self._counts.get(task_id, {})

    async def update_summary(self, task_id: str, values: dict[str, object]) -> None:
        self._tasks[task_id].summary.update(values)

    async def document_results_page(
        self,
        task_id: str,
        *,
        statuses: object = None,
        after_updated_at: datetime | None = None,
        after_document_id: str | None = None,
        limit: int = 50,
    ) -> TaskDocumentPage:
        del statuses
        rows = sorted(
            self.failed_documents.get(task_id, []), key=lambda r: (r.updated_at, r.document_id)
        )
        if after_updated_at is not None:
            rows = [
                r
                for r in rows
                if (r.updated_at, r.document_id) > (after_updated_at, after_document_id)
            ]
        return TaskDocumentPage(tuple(rows[:limit]), has_more=len(rows) > limit)

    async def append_task_event_once(self, task_id: str, event: HarborEvent) -> HarborEvent | None:
        if any(owner == task_id and stored.name == event.name for owner, stored in self.appended):
            return None
        return await self.append_task_event(task_id, event)

    async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent:
        seq = self._next_seq.get(task_id, 0) + 1
        self._next_seq[task_id] = seq
        stamped = replace(event, seq=seq)
        self.appended.append((task_id, stamped))
        return stamped


def _wire(store: FakeTaskStore, task_id: str = "t1") -> list[tuple[str, int | None, dict]]:
    prefix = f"task.{task_id}."
    return [
        (event.name.removeprefix(prefix), event.seq, event.payload)
        for owner, event in store.appended
        if owner == task_id
    ]


def _failed_document(document_id: str, **result: object) -> StoredTaskDocumentResult:
    return StoredTaskDocumentResult(
        task_id="t1",
        document_id=document_id,
        document_version_id=None,
        status="failed",
        result=dict(result),
        updated_at=datetime(2026, 11, 7, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_progress_change_emits_run_status_and_stage_progress_with_explorer_payloads() -> None:
    store = FakeTaskStore(
        [_FakeTask("t1", IngestionTaskState.RUNNING, summary={"discovered": 4})],
        {"t1": {"published": 1, "unchanged": 1}},
    )
    bus = InProcessEventBus()
    subscriber = bus.subscribe("task.t1.")

    examined = await sync_ingestion_progress(store, bus)

    assert examined == 1
    live = [await subscriber.__anext__(), await subscriber.__anext__()]
    assert live == [event for _, event in store.appended]
    assert _wire(store) == [
        (
            "run.status",
            1,
            {
                "status": "running",
                "counters": {"discovered": 4, "processed": 1, "skipped": 1, "failed": 0},
            },
        ),
        ("stage.progress", 2, {"stage": "publish", "done": 2, "total": 4}),
    ]


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
async def test_reaching_a_terminal_state_publishes_run_completed_live() -> None:
    task = _FakeTask("t1", IngestionTaskState.RUNNING, summary={"discovered": 2})
    store = FakeTaskStore(
        [task],
        {"t1": {"published": 2}},
        finish_on_progress={"t1": IngestionTaskState.COMPLETED},
    )
    bus = InProcessEventBus()
    subscriber = bus.subscribe("task.t1.")

    await sync_ingestion_progress(store, bus)

    names = [(await subscriber.__anext__()).name for _ in range(2)]
    assert names == ["task.t1.stage.progress", "task.t1.run.completed"]
    assert _wire(store)[-1][2] == {
        "counters": {"discovered": 2, "processed": 2, "skipped": 0, "failed": 0},
        "finish_reason": "succeeded",
    }


@pytest.mark.asyncio
async def test_a_run_that_progresses_fails_a_document_pauses_and_completes() -> None:
    task = _FakeTask("t1", IngestionTaskState.RUNNING, summary={"discovered": 3})
    store = FakeTaskStore([task], {"t1": {}})
    bus = InProcessEventBus()

    await sync_ingestion_progress(store, bus)
    store._counts["t1"] = {"published": 1, "failed": 1}
    store.failed_documents["t1"] = [
        _failed_document("doc-9", failure_stage="ParseAndNormalize", safe_error_code="parse_failed")
    ]
    await sync_ingestion_progress(store, bus)
    task.status = IngestionTaskState.PAUSED
    await sync_ingestion_progress(store, bus)
    await sync_ingestion_progress(store, bus)
    task.status = IngestionTaskState.RUNNING
    store._counts["t1"] = {"published": 2, "failed": 1}
    await sync_ingestion_progress(store, bus)
    task.status = IngestionTaskState.PARTIAL
    await sync_run(store, bus, task)

    wire = _wire(store)
    assert [name for name, _, _ in wire] == [
        "run.status",
        "stage.progress",
        "run.status",
        "stage.progress",
        "document.failed",
        "run.status",
        "run.status",
        "stage.progress",
        "run.completed",
    ]
    ids = [seq for _, seq, _ in wire]
    assert ids == sorted(set(ids))
    payloads = {name: payload for name, _, payload in wire}
    assert wire[4][2] == {
        "document_id": "doc-9",
        "stage": "interpret",
        "error_category": "parse_failed",
    }
    assert wire[5][2]["status"] == "paused"
    assert wire[6][2]["status"] == "running"
    assert payloads["run.completed"] == {
        "counters": {"discovered": 3, "processed": 2, "skipped": 0, "failed": 1},
        "finish_reason": "succeeded",
    }


@pytest.mark.asyncio
async def test_a_failed_document_is_emitted_once_across_ticks() -> None:
    task = _FakeTask("t1", IngestionTaskState.RUNNING, summary={"discovered": 3})
    store = FakeTaskStore([task], {"t1": {"failed": 1}})
    store.failed_documents["t1"] = [_failed_document("doc-a")]
    bus = InProcessEventBus()

    await sync_ingestion_progress(store, bus)
    store._counts["t1"] = {"failed": 2}
    store.failed_documents["t1"].append(_failed_document("doc-b"))
    await sync_ingestion_progress(store, bus)

    failed = [payload for name, _, payload in _wire(store) if name == "document.failed"]
    assert failed == [
        {"document_id": "doc-a", "stage": "capture", "error_category": "unknown"},
        {"document_id": "doc-b", "stage": "capture", "error_category": "unknown"},
    ]


@pytest.mark.asyncio
async def test_run_completed_is_appended_once_however_often_the_run_is_synced() -> None:
    task = _FakeTask("t1", IngestionTaskState.COMPLETED, summary={"discovered": 1})
    store = FakeTaskStore([task], {"t1": {"published": 1}})
    bus = InProcessEventBus()

    await sync_run(store, bus, task)
    await sync_run(store, bus, task)

    assert [name for name, _, _ in _wire(store)].count("run.completed") == 1
