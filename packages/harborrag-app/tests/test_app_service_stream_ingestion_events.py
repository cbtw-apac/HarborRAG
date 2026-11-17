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
from dataclasses import replace

import pytest
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
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
        del task_id
        return [e for e in self._backlog if after_seq is None or (e.seq or 0) > after_seq][:limit]


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
    "run.completed" event get persisted) in the gap between this method reading the
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
    done_event = HarborEvent(name="task.t1.run.completed", trace_id="trace-1")
    service = _build_service(bus, task, backlog=(done_event,))

    events = service.stream_ingestion_events("t1")
    received = [event async for event in events]

    assert received == [done_event]
    assert bus._subscribers == []


@pytest.mark.asyncio
async def test_a_live_event_already_replayed_from_the_backlog_is_not_repeated() -> None:
    """Subscribing before the backlog read can deliver one event twice; ids must only increase."""
    bus = InProcessEventBus()
    task = IngestionTask(
        task_id="t1",
        source_scope_id="scope-1",
        status=IngestionTaskState.RUNNING,
        request={"tenant_id": "DEFAULT", "connector_type": "confluence", "connection_id": "c1"},
    )
    status = HarborEvent(name="task.t1.run.status", trace_id="t1", seq=1)
    completed = HarborEvent(name="task.t1.run.completed", trace_id="t1", seq=2)
    service = _build_service(bus, task, backlog=(status,))

    events = service.stream_ingestion_events("t1")
    first = await events.__anext__()
    await bus.publish(status)
    await bus.publish(completed)
    rest = [event async for event in events]

    assert [first, *rest] == [status, completed]


@pytest.mark.asyncio
async def test_control_audit_events_are_stored_but_not_streamed() -> None:
    bus = InProcessEventBus()
    task = IngestionTask(
        task_id="t1",
        source_scope_id="scope-1",
        status=IngestionTaskState.RUNNING,
        request={"tenant_id": "DEFAULT", "connector_type": "confluence", "connection_id": "c1"},
    )
    audit = HarborEvent(name="task.t1.control", trace_id="t1", seq=1, payload={"actor": "dev"})
    completed = HarborEvent(name="task.t1.run.completed", trace_id="t1", seq=2)
    service = _build_service(bus, task, backlog=(audit, completed))

    received = [event async for event in service.stream_ingestion_events("t1")]

    assert received == [completed]


class _EventLogStore:
    """An in-memory run registry with a real event log, for the stream's self-completion."""

    def __init__(self, task: IngestionTask, events: list[HarborEvent] | None = None) -> None:
        self.task = task
        self.events = list(events or [])

    async def get(self, task_id: str) -> IngestionTask | None:
        return self.task if task_id == self.task.task_id else None

    async def progress(self, task_id: str) -> dict[str, int]:
        del task_id
        return {"published": 1}

    async def update_summary(self, task_id: str, values: dict[str, object]) -> None:
        del task_id
        self.task = self.task.model_copy(update={"summary": {**self.task.summary, **values}})

    async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent:
        del task_id
        stamped = replace(event, seq=len(self.events) + 1)
        self.events.append(stamped)
        return stamped

    async def append_task_event_once(self, task_id: str, event: HarborEvent) -> HarborEvent | None:
        if any(stored.name == event.name for stored in self.events):
            return None
        await asyncio.sleep(0)
        if any(stored.name == event.name for stored in self.events):
            return None
        return await self.append_task_event(task_id, event)

    async def list_task_events(
        self, task_id: str, *, after_seq: int | None = None, limit: int = 500
    ) -> list[HarborEvent]:
        del task_id
        return [e for e in self.events if e.seq > (after_seq or 0)][:limit]


def _service_over(store: _EventLogStore, bus: InProcessEventBus) -> AppService:
    async def registry_factory(_settings):
        return store

    return AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(task_registry=registry_factory, event_bus=lambda: bus),
    )


def _ended_task(status: IngestionTaskState) -> IngestionTask:
    return IngestionTask(
        task_id="t1",
        source_scope_id="scope-1",
        status=status,
        request={"tenant_id": "DEFAULT", "connector_type": "confluence", "connection_id": "c1"},
        summary={"discovered": 1},
    )


@pytest.mark.asyncio
async def test_a_run_that_ended_unseen_gets_one_run_completed_for_a_new_stream() -> None:
    store = _EventLogStore(_ended_task(IngestionTaskState.COMPLETED))
    service = _service_over(store, InProcessEventBus())

    received = [event async for event in service.stream_ingestion_events("t1")]

    assert [e.name for e in received][-1] == "task.t1.run.completed"
    assert [e.name for e in store.events].count("task.t1.run.completed") == 1
    assert received[-1].payload["finish_reason"] == "succeeded"
    assert [e.seq for e in received] == sorted({e.seq for e in received})


@pytest.mark.asyncio
async def test_a_live_watcher_gets_run_completed_when_the_run_ends_between_bridge_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "harborrag_app.workflow_control.composition.service._END_CHECK_SECONDS", 0.01
    )
    task = _ended_task(IngestionTaskState.RUNNING)
    store = _EventLogStore(task)
    service = _service_over(store, InProcessEventBus())

    async def finish() -> None:
        await asyncio.sleep(0.05)
        store.task = task.model_copy(update={"status": IngestionTaskState.COMPLETED})

    finisher = asyncio.ensure_future(finish())
    received = await asyncio.wait_for(_collect(service.stream_ingestion_events("t1")), timeout=2)
    await finisher

    assert received[-1].name == "task.t1.run.completed"


async def _collect(events):
    return [event async for event in events]


@pytest.mark.asyncio
async def test_two_concurrent_streams_store_one_run_completed() -> None:
    store = _EventLogStore(_ended_task(IngestionTaskState.COMPLETED))
    service = _service_over(store, InProcessEventBus())

    first, second = await asyncio.gather(
        _collect(service.stream_ingestion_events("t1")),
        _collect(service.stream_ingestion_events("t1")),
    )

    assert [e.name for e in store.events].count("task.t1.run.completed") == 1
    assert first[-1].name == second[-1].name == "task.t1.run.completed"
    assert first[-1].seq == second[-1].seq


@pytest.mark.asyncio
async def test_replay_pages_past_five_hundred_events_through_run_completed() -> None:
    task = _ended_task(IngestionTaskState.COMPLETED)
    events = [
        HarborEvent(name="task.t1.document.failed", trace_id="t1", seq=index)
        for index in range(1, 1201)
    ]
    events.append(HarborEvent(name="task.t1.run.completed", trace_id="t1", seq=1201))
    service = _service_over(_EventLogStore(task, events), InProcessEventBus())

    received = await _collect(service.stream_ingestion_events("t1"))

    assert [e.seq for e in received] == list(range(1, 1202))
    assert received[-1].name == "task.t1.run.completed"


@pytest.mark.asyncio
async def test_run_completed_is_sent_even_when_a_later_duplicate_was_delivered_first() -> None:
    """A higher-sequence event can overtake ``run.completed`` on the bus; it must not be lost."""
    bus = InProcessEventBus()
    task = _ended_task(IngestionTaskState.RUNNING)
    store = _EventLogStore(task)
    service = _service_over(store, bus)
    stage = HarborEvent(name="task.t1.stage.progress", trace_id="t1", seq=5)
    completed = HarborEvent(name="task.t1.run.completed", trace_id="t1", seq=4)
    store.events.extend([replace(stage, seq=1), replace(completed, seq=2)])
    store.events.clear()

    events = service.stream_ingestion_events("t1", after_seq=4)
    consume = asyncio.ensure_future(_collect(events))
    await asyncio.sleep(0.01)
    await bus.publish(stage)
    await bus.publish(completed)
    received = await asyncio.wait_for(consume, timeout=2)

    assert [e.name for e in received] == ["task.t1.stage.progress", "task.t1.run.completed"]


@pytest.mark.asyncio
async def test_events_the_live_tail_skipped_are_read_back_from_the_store() -> None:
    bus = InProcessEventBus()
    store = _EventLogStore(_ended_task(IngestionTaskState.RUNNING))
    service = _service_over(store, bus)
    stored = [
        HarborEvent(name="task.t1.document.failed", trace_id="t1", seq=1),
        HarborEvent(name="task.t1.document.failed", trace_id="t1", seq=2),
        HarborEvent(name="task.t1.document.failed", trace_id="t1", seq=3),
        HarborEvent(name="task.t1.run.completed", trace_id="t1", seq=4),
    ]

    consume = asyncio.ensure_future(_collect(service.stream_ingestion_events("t1")))
    await asyncio.sleep(0.01)
    store.events.extend(stored)
    await bus.publish(stored[2])  # events 1 and 2 never reached this subscriber
    await bus.publish(stored[3])
    received = await asyncio.wait_for(consume, timeout=2)

    assert [e.seq for e in received] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_events_stored_under_retired_names_are_not_streamed() -> None:
    task = _ended_task(IngestionTaskState.COMPLETED)
    events = [
        HarborEvent(name="task.t1.progress", trace_id="t1", seq=1),
        HarborEvent(name="task.t1.run.status", trace_id="t1", seq=2),
        HarborEvent(name="task.t1.done", trace_id="t1", seq=3),
        HarborEvent(name="task.t1.run.completed", trace_id="t1", seq=4),
    ]
    service = _service_over(_EventLogStore(task, events), InProcessEventBus())

    received = await _collect(service.stream_ingestion_events("t1"))

    assert [e.name for e in received] == ["task.t1.run.status", "task.t1.run.completed"]
