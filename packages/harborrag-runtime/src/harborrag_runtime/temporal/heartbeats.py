from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable
from typing import cast

from temporalio import activity

logger = logging.getLogger("harborrag.runtime.temporal.heartbeats")


def last_heartbeat_detail() -> object | None:
    """Return the last serialized heartbeat payload from a prior attempt."""
    if not activity.in_activity():
        return None
    details = activity.info().heartbeat_details
    if not details:
        return None
    try:
        return cast(object, details[0])
    except (IndexError, TypeError):
        return None


def _require_positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive, got {value!r}")


def _require_shorter_than_timeouts(interval_seconds: float) -> None:
    info = activity.info()
    for label, timeout in (
        ("start_to_close_timeout", info.start_to_close_timeout),
        ("heartbeat_timeout", info.heartbeat_timeout),
    ):
        if timeout is not None and timeout.total_seconds() > 0:
            limit = timeout.total_seconds()
            if interval_seconds >= limit:
                raise ValueError(
                    f"heartbeat interval ({interval_seconds}s) must be shorter than "
                    f"{label} ({limit:.0f}s)"
                )


async def heartbeat_while[ResultT](
    operation: Awaitable[ResultT],
    *,
    detail: object,
    interval_seconds: float = 30.0,
    stop_after_seconds: float | None = None,
) -> ResultT:
    """Heartbeat a long operation without changing service-layer APIs.

    ``stop_after_seconds`` is a backstop for work with its own hard deadline
    (an isolated parse): once that much time has passed, heartbeats stop, so
    if the work somehow outlived its kill, Temporal's heartbeat timeout fails
    the attempt instead of the attempt being heartbeated indefinitely.
    """

    _require_positive("heartbeat interval", interval_seconds)
    if stop_after_seconds is not None:
        _require_positive("heartbeat budget", stop_after_seconds)
    if not activity.in_activity():
        return await operation
    _require_shorter_than_timeouts(interval_seconds)
    activity.heartbeat(detail)
    loop = asyncio.get_running_loop()
    stop_at = None if stop_after_seconds is None else loop.time() + stop_after_seconds

    async def pulse() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            if stop_at is not None and loop.time() >= stop_at:
                logger.warning(
                    "Heartbeats stopped: operation ran past its %.0fs budget detail=%s",
                    stop_after_seconds,
                    detail,
                )
                return
            activity.heartbeat(detail)

    pulse_task = asyncio.create_task(pulse())
    try:
        return await operation
    finally:
        pulse_task.cancel()
        await asyncio.gather(pulse_task, return_exceptions=True)
