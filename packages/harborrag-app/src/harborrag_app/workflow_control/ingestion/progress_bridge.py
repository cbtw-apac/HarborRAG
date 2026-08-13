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
from collections.abc import Awaitable, Callable

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
_LIST_ACTIVE_PAGE_LIMIT = 500

# Named lease this bridge's tick runs under (LeaseRepositoryPort) so only one
# process ticks it when the API is scaled to multiple processes/replicas --
# without this, N processes diff the same tasks concurrently and each one's
# read-then-write snapshot check races the others into appending duplicate
# progress rows (distinct by seq, never deduped). Must match the lease name
# seeded by control_plane migration 0017. TTL is a few tick intervals so a
# crashed leader's lease lapses and a live process picks the work back up
# quickly, without either process's own timer jitter costing it the lease.
LEASE_NAME = "ingestion_progress_bridge"
LEASE_TTL_SECONDS = _DEFAULT_TICK_INTERVAL_SECONDS * 4


async def sync_ingestion_progress(
    store: PublicTaskStore,
    event_bus: EventBusPort,
    *,
    still_leader: Callable[[], Awaitable[bool]] | None = None,
) -> int:
    """One poll tick: diff every active task's progress snapshot.

    For each task whose counts changed since the last tick, append + publish
    a "task.<id>.progress" event. For a task that reached a terminal status
    since the last tick, append + publish a final "task.<id>.done" event
    too -- the stream route's stop signal. Returns the number of active
    tasks examined.

    list_active() is a bounded, keyset-paginated read, not a single snapshot
    of every active task -- with more active tasks than fit in one page,
    stopping after the first page would silently starve later tasks of
    progress/done events. So this walks every page each tick, following the
    (submitted_at, task_id) cursor from the last row of each page until a
    short page signals the end.

    ``still_leader``, when given, is re-checked before every page (an
    AppService caller passes its lease's try_acquire, which re-confirms
    ownership and renews the lease's ttl in one call). A single ownership
    check up front, before this pagination existed, was enough because one
    tick's work was small and fast; now that a tick can span an unbounded
    number of pages, a large enough backlog of active tasks could make one
    tick outlive the lease's ttl, letting another process take over the
    same lease and start its own overlapping drain while this one is still
    running. If ownership is no longer confirmed, this stops early and
    returns however many tasks were examined so far -- safe, because the
    next tick (by whichever process now holds the lease) restarts the
    pagination from the beginning and re-diffs every active task's
    snapshot, so nothing already published needs redoing and nothing
    skipped here is lost.
    """
    examined = 0
    after_submitted_at = None
    after_task_id = None
    while True:
        if still_leader is not None and not await still_leader():
            logger.info(
                "Ingestion progress bridge lost the lease mid-tick after examining %d tasks; "
                "stopping early",
                examined,
            )
            break
        page = await store.list_active(
            after_submitted_at=after_submitted_at,
            after_task_id=after_task_id,
            limit=_LIST_ACTIVE_PAGE_LIMIT,
        )
        if not page:
            break
        for task in page:
            counts = await store.progress(task.task_id)
            if counts != task.summary.get(_SNAPSHOT_KEY):
                event = HarborEvent(
                    name=f"task.{task.task_id}.progress",
                    trace_id=task.task_id,
                    payload={"status": task.status.value, "counts": counts},
                )
                event = await store.append_task_event(task.task_id, event)
                await event_bus.publish(event)
                await store.update_summary(task.task_id, {_SNAPSHOT_KEY: counts})
            refreshed = await store.get(task.task_id)
            if refreshed is not None and refreshed.status in _TERMINAL_STATES:
                done_event = HarborEvent(
                    name=f"task.{task.task_id}.done",
                    trace_id=task.task_id,
                    payload={"status": refreshed.status.value, "counts": counts},
                )
                done_event = await store.append_task_event(task.task_id, done_event)
                await event_bus.publish(done_event)
        examined += len(page)
        if len(page) < _LIST_ACTIVE_PAGE_LIMIT:
            break
        after_submitted_at = page[-1].submitted_at
        after_task_id = page[-1].task_id
    return examined


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
