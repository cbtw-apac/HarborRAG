"""Terminal SSE delivery and task identity across streamed frames."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from harborrag_app.api.sse import bounded_sse_frames, sse_frame


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["response.completed", "response.error", "result"])
async def test_terminal_frame_does_not_gain_a_timeout_error_during_slow_delivery(
    terminal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = False

    async def source() -> AsyncGenerator[bytes, None]:
        nonlocal closed
        try:
            yield sse_frame(terminal, {"id": "answer-1"})
            pytest.fail("a terminal frame must close its source without requesting another frame")
        finally:
            closed = True

    loop = asyncio.get_running_loop()
    now = loop.time()
    monkeypatch.setattr(loop, "time", lambda: now)
    stream = bounded_sse_frames(
        source(),
        timeout_seconds=1,
        error_message="deadline exceeded",
        terminal_events=(terminal,),
    )
    assert await anext(stream) == sse_frame(terminal, {"id": "answer-1"})
    # Sending the yielded bytes can take longer than the remaining deadline.
    now += 2
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert closed


@pytest.mark.asyncio
async def test_frame_production_keeps_the_consumers_task_identity() -> None:
    owner = asyncio.current_task()
    observed: list[asyncio.Task[object] | None] = []

    async def source() -> AsyncGenerator[bytes, None]:
        for index in range(3):
            observed.append(asyncio.current_task())
            await asyncio.sleep(0)
            yield sse_frame("progress", {"index": index})

    stream = bounded_sse_frames(source(), timeout_seconds=1, error_message="deadline exceeded")
    assert len([frame async for frame in stream]) == 3
    # A lease heartbeat must cancel this same live task on every provider read.
    assert observed == [owner, owner, owner]
