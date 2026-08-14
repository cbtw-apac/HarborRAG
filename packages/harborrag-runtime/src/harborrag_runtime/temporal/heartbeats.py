from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from temporalio import activity


def last_heartbeat_detail() -> object | None:
    """Return the last serialized heartbeat payload from a prior attempt."""
    if not activity.in_activity():
        return None
    details = activity.info().heartbeat_details
    if not details:
        return None
    try:
        return details[0]
    except (IndexError, TypeError):
        return None


async def heartbeat_while[ResultT](
    operation: Awaitable[ResultT],
    *,
    detail: object,
    interval_seconds: float = 30.0,
) -> ResultT:
    """Heartbeat a long operation without changing service-layer APIs."""
    if interval_seconds <= 0:
        raise ValueError(f"heartbeat interval must be positive, got {interval_seconds!r}")

    if not activity.in_activity():
        return await operation

    info = activity.info()
    if info.start_to_close_timeout is not None and info.start_to_close_timeout.total_seconds() > 0:
        limit = info.start_to_close_timeout.total_seconds()
        if interval_seconds >= limit:
            raise ValueError(
                f"heartbeat interval ({interval_seconds}s) must be shorter than "
                f"start_to_close_timeout ({limit:.0f}s)"
            )

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
