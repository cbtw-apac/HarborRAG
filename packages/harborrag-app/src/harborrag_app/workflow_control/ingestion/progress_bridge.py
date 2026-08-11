"""Poll active ingestion tasks and fan progress out via the event bus (ML2).

The new task model (IngestionApplicationService/PublicTaskStore) has no
push mechanism -- progress() is a point-in-time snapshot recomputed on each
call. This bridge is the diff loop that turns those snapshots into an
ordered, replayable event stream: mirrors the job-domain's
sync_job_progress/run_job_progress_bridge design one-for-one, retargeted at
tasks instead of jobs.
"""

from __future__ import annotations

import asyncio
import logging

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.ports.events import EventBusPort

from .ports import PublicTaskStore

logger = logging.getLogger("harborrag.app.workflow_control.ingestion.progress_bridge")

_TERMINAL_STATES = frozenset(
    {
        IngestionTaskState.COMPLETED,
        IngestionTaskState.PARTIAL,
        IngestionTaskState.FAILED,
        IngestionTaskState.CANCELLED,
    }
)
_SNAPSHOT_KEY = "_last_progress_snapshot"
_DEFAULT_TICK_INTERVAL_SECONDS = 2.0


async def sync_ingestion_progress(store: PublicTaskStore, event_bus: EventBusPort) -> int:
    """One poll tick: diff every active task's progress snapshot.

    For each task whose counts changed since the last tick, append + publish
    a "task.<id>.progress" event. For a task that reached a terminal status
    since the last tick, append + publish a final "task.<id>.done" event
    too -- the stream route's stop signal. Returns the number of active
    tasks examined.
    """
    active = await store.list_active()
    for task in active:
        counts = await store.progress(task.task_id)
        if counts != task.summary.get(_SNAPSHOT_KEY):
            event = HarborEvent(
                name=f"task.{task.task_id}.progress",
                trace_id=task.task_id,
                payload={"status": task.status.value, "counts": counts},
            )
            await store.append_task_event(task.task_id, event)
            await event_bus.publish(event)
            await store.update_summary(task.task_id, {_SNAPSHOT_KEY: counts})
        refreshed = await store.get(task.task_id)
        if refreshed is not None and refreshed.status in _TERMINAL_STATES:
            done_event = HarborEvent(
                name=f"task.{task.task_id}.done",
                trace_id=task.task_id,
                payload={"status": refreshed.status.value, "counts": counts},
            )
            await store.append_task_event(task.task_id, done_event)
            await event_bus.publish(done_event)
    return len(active)


async def run_ingestion_progress_bridge(
    store: PublicTaskStore,
    event_bus: EventBusPort,
    *,
    interval_seconds: float = _DEFAULT_TICK_INTERVAL_SECONDS,
) -> None:
    """Run sync_ingestion_progress on a fixed interval until cancelled.

    Each tick is independently supervised: one bad tick (a transient DB
    error, a task that vanished mid-poll) is logged and skipped rather than
    killing the whole background task, so a single failure can't silently
    stop all future progress delivery for the process's lifetime.
    """
    while True:
        try:
            await sync_ingestion_progress(store, event_bus)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ingestion progress sync tick failed")
        await asyncio.sleep(interval_seconds)
