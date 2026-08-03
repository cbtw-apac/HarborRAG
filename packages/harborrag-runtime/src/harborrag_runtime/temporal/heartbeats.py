from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from temporalio import activity


async def heartbeat_while[ResultT](
    operation: Awaitable[ResultT],
    *,
    detail: str,
    interval_seconds: float = 30.0,
) -> ResultT:
    """Heartbeat a long operation without changing service-layer APIs."""

    if not activity.in_activity():
        return await operation

    activity.heartbeat(detail)

    async def pulse() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            activity.heartbeat(detail)

    pulse_task = asyncio.create_task(pulse())
    try:
        return await operation
    finally:
        pulse_task.cancel()
        await asyncio.gather(pulse_task, return_exceptions=True)
