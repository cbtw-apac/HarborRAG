"""InProcessEventBus: the real (not fake) EventBusPort adapter (ML2 P3)."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_adapters.events.in_process import InProcessEventBus
from harborrag_core.contracts.events import HarborEvent


@pytest.mark.asyncio
async def test_subscribe_receives_events_published_after_it_starts() -> None:
    bus = InProcessEventBus()
    events = bus.subscribe("job.1.")
    published = HarborEvent(name="job.1.progress", trace_id="job-1", payload={"n": 1})
    await bus.publish(published)
    received = await events.__anext__()
    assert received == published


@pytest.mark.asyncio
async def test_subscribe_filters_by_name_prefix() -> None:
    bus = InProcessEventBus()
    events = bus.subscribe("job.1.")
    await bus.publish(HarborEvent(name="job.2.progress", trace_id="job-2"))
    matching = HarborEvent(name="job.1.progress", trace_id="job-1")
    await bus.publish(matching)
    received = await events.__anext__()
    assert received == matching


@pytest.mark.asyncio
async def test_publish_fans_out_to_multiple_concurrent_subscribers() -> None:
    bus = InProcessEventBus()
    first = bus.subscribe("job.1.")
    second = bus.subscribe("job.1.")
    event = HarborEvent(name="job.1.progress", trace_id="job-1")
    await bus.publish(event)
    assert await first.__anext__() == event
    assert await second.__anext__() == event


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_does_not_raise() -> None:
    bus = InProcessEventBus()
    await bus.publish(HarborEvent(name="job.1.progress", trace_id="job-1"))


@pytest.mark.asyncio
async def test_subscriber_is_deregistered_on_task_cancellation() -> None:
    bus = InProcessEventBus()

    async def _consume() -> None:
        async for _event in bus.subscribe("job.1."):
            pass

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0)  # let the subscription register
    assert len(bus._subscribers) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus._subscribers == []
