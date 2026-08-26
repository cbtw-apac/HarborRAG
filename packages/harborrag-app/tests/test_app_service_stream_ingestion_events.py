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

    async def list_task_events(self, task_id: str, *, after_seq: int | None = None):
        del task_id, after_seq
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


@pytest.mark.asyncio
async def test_reconciled_terminal_status_skips_live_subscription() -> None:
    """If reconciliation marks a task terminal, stream must not open a live tail."""
    bus = InProcessEventBus()
    task = IngestionTask(
        task_id="t1",
        source_scope_id="scope-1",
        status=IngestionTaskState.PENDING,
        request={
            "tenant_id": "DEFAULT",
            "connector_type": "confluence",
            "connection_id": "c1",
        },
    )
    service = _build_service(bus, task)

    async def _reconciled_terminal(_task_id: str) -> dict[str, object]:
        return {
            "task_id": "t1",
            "tenant": "DEFAULT",
            "status": "FAILED",
            "stage": "COMPLETED",
        }

    service._public_ingestions.get_task = _reconciled_terminal

    received = await asyncio.wait_for(
        _collect(service.stream_ingestion_events("t1")),
        timeout=0.25,
    )

    assert received == []
    assert bus._subscribers == []


async def _collect(events) -> list[HarborEvent]:
    return [event async for event in events]
