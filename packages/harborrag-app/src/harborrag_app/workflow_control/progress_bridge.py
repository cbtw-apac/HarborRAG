"""Background loop bridging Temporal's pull-only job status into the event bus.

Independent of any client connection, per the reconnect-replay requirement:
a job's progress must keep landing in job_events even while nobody is
currently streaming it, so a client that reconnects later can replay what it
missed. Started/stopped from the API process's lifespan (api/app.py).
"""

from __future__ import annotations

import asyncio
import logging

from .ports import BaseAppService

logger = logging.getLogger("harborrag.app.workflow_control.progress_bridge")

_DEFAULT_POLL_INTERVAL_SECONDS = 2.0


async def run_job_progress_bridge(
    service: BaseAppService,
    *,
    interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """Call service.sync_job_progress() every interval_seconds, forever.

    Runs as a long-lived asyncio task; the caller cancels it on shutdown. A
    failed tick is logged and skipped rather than killing the loop -- a
    transient Temporal/DB hiccup shouldn't stop future ticks from recovering.
    """

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await service.sync_job_progress()
        except Exception:  # noqa: BLE001 - keep the loop alive across bad ticks
            logger.exception("Job progress sync tick failed")
