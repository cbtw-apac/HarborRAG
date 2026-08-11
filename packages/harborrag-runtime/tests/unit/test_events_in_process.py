"""InProcessEventBus: fan-out, cancellation cleanup, and bounded queues."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_core.contracts.events import HarborEvent
from harborrag_runtime.events.in_process import InProcessEventBus


@pytest.mark.asyncio
async def test_publish_fans_out_to_multiple_matching_subscribers() -> None:
    bus = InProcessEventBus()
    a = bus.subscribe("job.1.")
    b = bus.subscribe("job.1.")
    other = bus.subscribe("job.2.")

    await bus.publish(HarborEvent(name="job.1.progress", trace_id="t1"))

    assert (await a.__anext__()).name == "job.1.progress"
    assert (await b.__anext__()).name == "job.1.progress"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(other.__anext__(), timeout=0.05)


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_does_not_raise() -> None:
    bus = InProcessEventBus()
    await bus.publish(HarborEvent(name="job.1.progress", trace_id="job-1"))


@pytest.mark.asyncio
async def test_subscriber_is_deregistered_on_task_cancellation() -> None:
    bus = InProcessEventBus()
    registered = asyncio.Event()

    async def _consume() -> None:
        async for _event in bus.subscribe("job.1."):
            pass

    async def _consume_and_signal() -> None:
        stream = bus.subscribe("job.1.")
        registered.set()
        async for _event in stream:
            pass

    task = asyncio.create_task(_consume_and_signal())
    await registered.wait()
    assert len(bus._subscribers) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus._subscribers == []


@pytest.mark.asyncio
async def test_full_queue_drops_the_oldest_progress_event_not_publish() -> None:
    """publish() never blocks: a stalled subscriber's queue coalesces instead."""
    bus = InProcessEventBus(max_queue_size=2)
    stream = bus.subscribe("job.1.")

    for index in range(5):
        await asyncio.wait_for(
            bus.publish(HarborEvent(name="job.1.progress", trace_id=f"t{index}")), timeout=0.1
        )

    first = await stream.__anext__()
    second = await stream.__anext__()
    assert (first.trace_id, second.trace_id) == ("t3", "t4")


@pytest.mark.asyncio
async def test_full_queue_still_admits_a_terminal_done_event() -> None:
    bus = InProcessEventBus(max_queue_size=2)
    stream = bus.subscribe("job.1.")

    await bus.publish(HarborEvent(name="job.1.progress", trace_id="t1"))
    await bus.publish(HarborEvent(name="job.1.progress", trace_id="t2"))
    await asyncio.wait_for(bus.publish(HarborEvent(name="job.1.done", trace_id="t3")), timeout=0.1)

    events = [await stream.__anext__(), await stream.__anext__()]
    assert events[-1].name == "job.1.done"
