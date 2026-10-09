"""AppService.stream_ingestion_events must not leak its live subscription.

Regression coverage for a coderabbit finding: the generator subscribes to
the event bus before replaying the backlog (by design -- see the method's
docstring), but if the caller abandons the generator before it ever reaches
the live tail (e.g. an SSE client disconnecting mid-backlog), nothing used
to close that subscription. It sat registered until the event bus's
GC-driven weakref finalizer happened to run.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_app.workflow_control.ingestion import event_stream
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_runtime.events.in_process import InProcessEventBus


class _FakeTaskStore:
    """Just enough of PublicTaskStore for stream_ingestion_events to drive."""

    def __init__(self, task: IngestionTask, *, backlog: tuple[HarborEvent, ...] = ()) -> None:
        self._task = task
        self._backlog = backlog

    async def get(self, task_id: str) -> IngestionTask | None:
        return self._task if task_id == self._task.task_id else None

    async def progress(self, task_id: str) -> dict[str, int]:
        del task_id
        return {}

    async def list_task_events(
        self, task_id: str, *, after_seq: int | None = None, limit: int = 500
    ):
        del task_id, after_seq, limit
        return self._backlog


def _build_service(
    bus: InProcessEventBus, task: IngestionTask, *, backlog: tuple[HarborEvent, ...] = ()
) -> AppService:
    store = _FakeTaskStore(task, backlog=backlog)

    async def registry_factory(_settings):
        return store

    return AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            task_registry=registry_factory,
            event_bus=lambda: bus,
        ),
    )


@pytest.mark.asyncio
async def test_abandoning_the_stream_mid_backlog_deregisters_the_live_subscription() -> None:
    """Cancelling the caller before any live event arrives must still unsubscribe."""
    bus = InProcessEventBus()
    task = IngestionTask(
        task_id="t1",
        source_scope_id="scope-1",
        status=IngestionTaskState.RUNNING,
        request={
            "tenant_id": "DEFAULT",
            "connector_type": "confluence",
            "connection_id": "c1",
        },
    )
    service = _build_service(bus, task)

    events = service.stream_ingestion_events("t1")
    consume = asyncio.ensure_future(events.__anext__())
    await asyncio.sleep(0)  # let it run: empty backlog, then block awaiting a live event
    assert len(bus._subscribers) == 1

    consume.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consume

    assert bus._subscribers == []


@pytest.mark.asyncio
async def test_backlog_done_event_is_yielded_and_ends_the_stream() -> None:
    """Regression for a coderabbit finding: the task can finish (and its
    ".done" event get persisted) in the gap between this method reading the
    task's status and it registering the live subscription. That leaves the
    terminal event sitting only in the backlog, never on the live bus. The
    backlog loop must yield it -- not just check its name and stop -- or the
    caller loses every persisted event, and then hang forever awaiting a live
    event that was never going to arrive.
    """
    bus = InProcessEventBus()
    task = IngestionTask(
        task_id="t1",
        source_scope_id="scope-1",
        status=IngestionTaskState.RUNNING,
        request={
            "tenant_id": "DEFAULT",
            "connector_type": "confluence",
            "connection_id": "c1",
        },
    )
    done_event = HarborEvent(name="task.t1.done", trace_id="trace-1")
    service = _build_service(bus, task, backlog=(done_event,))

    events = service.stream_ingestion_events("t1")
    received = [event async for event in events]

    assert received == [done_event]
    assert bus._subscribers == []


def _task(status: IngestionTaskState) -> IngestionTask:
    return IngestionTask(
        task_id="t1",
        source_scope_id="scope-1",
        status=status,
        request={"tenant_id": "DEFAULT", "connector_type": "confluence", "connection_id": "c1"},
    )


class _MutableTaskStore:
    """A store whose task status the test flips, and whose backlog honours paging."""

    def __init__(self, task: IngestionTask, *, backlog: list[HarborEvent] | None = None) -> None:
        self.task = task
        self.backlog = list(backlog or [])
        self.pages: list[tuple[int | None, int]] = []

    async def get(self, task_id: str) -> IngestionTask | None:
        return self.task if task_id == self.task.task_id else None

    async def progress(self, task_id: str) -> dict[str, int]:
        del task_id
        return {"succeeded": 3}

    async def list_task_events(
        self, task_id: str, *, after_seq: int | None = None, limit: int = 500
    ) -> list[HarborEvent]:
        del task_id
        self.pages.append((after_seq, limit))
        floor = -1 if after_seq is None else after_seq
        return [event for event in self.backlog if (event.seq or 0) > floor][:limit]


def _service_over(bus: InProcessEventBus, store: _MutableTaskStore) -> AppService:
    async def registry_factory(_settings):
        return store

    return AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(task_registry=registry_factory, event_bus=lambda: bus),
    )


@pytest.mark.asyncio
async def test_backlog_replay_pages_past_one_store_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_stream, "BACKLOG_PAGE_LIMIT", 2)
    backlog = [
        HarborEvent(name="task.t1.progress", trace_id="t1", payload={"n": n}, seq=n)
        for n in range(1, 5)
    ]
    backlog.append(HarborEvent(name="task.t1.done", trace_id="t1", seq=5))
    store = _MutableTaskStore(_task(IngestionTaskState.COMPLETED), backlog=backlog)
    service = _service_over(InProcessEventBus(), store)

    received = [event async for event in service.stream_ingestion_events("t1")]

    assert [event.seq for event in received] == [1, 2, 3, 4, 5]
    assert store.pages == [(None, 2), (2, 2), (4, 2)]


@pytest.mark.asyncio
async def test_a_task_that_finishes_without_a_live_done_event_still_ends_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review focus: the bridge's .done can be missed (lease handoff, bridge
    down). The tail must notice the terminal status itself, emit a closing
    .done, and release its bus subscription instead of hanging forever."""
    monkeypatch.setattr(event_stream, "STATUS_POLL_SECONDS", 0.01)
    bus = InProcessEventBus()
    store = _MutableTaskStore(_task(IngestionTaskState.RUNNING))
    events = _service_over(bus, store).stream_ingestion_events("t1")

    first = asyncio.ensure_future(events.__anext__())
    await asyncio.sleep(0.05)  # several polls while still RUNNING: nothing to yield
    assert not first.done()
    assert len(bus._subscribers) == 1
    store.task = store.task.model_copy(update={"status": IngestionTaskState.COMPLETED})
    done = await asyncio.wait_for(first, timeout=2)

    assert done.name == "task.t1.done"
    assert done.payload == {
        "status": IngestionTaskState.COMPLETED.value,
        "counts": {"succeeded": 3},
    }
    assert done.seq is None
    with pytest.raises(StopAsyncIteration):
        await events.__anext__()
    assert bus._subscribers == []


@pytest.mark.asyncio
async def test_a_persisted_done_found_at_terminal_time_is_replayed_not_synthesized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_stream, "STATUS_POLL_SECONDS", 0.01)
    bus = InProcessEventBus()
    store = _MutableTaskStore(_task(IngestionTaskState.RUNNING))
    events = _service_over(bus, store).stream_ingestion_events("t1")

    first = asyncio.ensure_future(events.__anext__())
    await asyncio.sleep(0.03)
    store.backlog.append(HarborEvent(name="task.t1.done", trace_id="t1", payload={"x": 1}, seq=7))
    store.task = store.task.model_copy(update={"status": IngestionTaskState.FAILED})
    done = await asyncio.wait_for(first, timeout=2)

    assert done.seq == 7
    assert done.payload == {"x": 1}


@pytest.mark.asyncio
async def test_a_live_done_event_still_ends_the_stream_before_any_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_stream, "STATUS_POLL_SECONDS", 30.0)
    bus = InProcessEventBus()
    store = _MutableTaskStore(_task(IngestionTaskState.RUNNING))
    events = _service_over(bus, store).stream_ingestion_events("t1")

    first = asyncio.ensure_future(events.__anext__())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await bus.publish(HarborEvent(name="task.t1.done", trace_id="t1", seq=3))

    assert (await asyncio.wait_for(first, timeout=2)).seq == 3
    with pytest.raises(StopAsyncIteration):
        await events.__anext__()
    assert bus._subscribers == []


@pytest.mark.asyncio
async def test_terminal_task_with_only_progress_backlog_still_ends_with_done() -> None:
    backlog = [
        HarborEvent(name="task.t1.progress", trace_id="t1", payload={"n": n}, seq=n) for n in (1, 2)
    ]
    store = _MutableTaskStore(_task(IngestionTaskState.COMPLETED), backlog=backlog)
    service = _service_over(InProcessEventBus(), store)

    received = [event async for event in service.stream_ingestion_events("t1")]

    assert [event.name for event in received] == [
        "task.t1.progress",
        "task.t1.progress",
        "task.t1.done",
    ]
    assert received[-1].seq is None
    assert received[-1].payload == {
        "status": IngestionTaskState.COMPLETED.value,
        "counts": {"succeeded": 3},
    }


@pytest.mark.asyncio
async def test_terminal_task_resumed_past_every_event_gets_a_synthesized_done() -> None:
    backlog = [HarborEvent(name="task.t1.progress", trace_id="t1", seq=1)]
    store = _MutableTaskStore(_task(IngestionTaskState.FAILED), backlog=backlog)
    service = _service_over(InProcessEventBus(), store)

    received = [event async for event in service.stream_ingestion_events("t1", after_seq=1)]

    assert [event.name for event in received] == ["task.t1.done"]


@pytest.mark.asyncio
async def test_terminal_task_whose_backlog_has_done_gets_no_second_done() -> None:
    backlog = [
        HarborEvent(name="task.t1.progress", trace_id="t1", seq=1),
        HarborEvent(name="task.t1.done", trace_id="t1", seq=2),
    ]
    store = _MutableTaskStore(_task(IngestionTaskState.COMPLETED), backlog=backlog)
    service = _service_over(InProcessEventBus(), store)

    received = [event async for event in service.stream_ingestion_events("t1")]

    assert [event.name for event in received] == ["task.t1.progress", "task.t1.done"]
