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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
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
# Durable marker that this task's terminal ".done" was already published; it
# stores the terminal status for information only (nothing depends on it
# changing). It deduplicates .done across the same-tick path, the vanished
# path and lease handoffs.
DONE_MARKER_KEY = "_done_published"
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


@dataclass
class ActiveTaskTracker:
    """Task ids this process saw active on its previous complete tick.

    list_active() only returns PENDING/RUNNING tasks, so a task that finishes
    during the sleep between ticks silently drops out of it. Remembering what
    was active lets the next tick re-read exactly those tasks and publish
    their terminal ``.done``. In-process and best effort: a process that has
    just taken the lease over starts empty, which is why the SSE stream also
    polls task status as its own backstop (event_stream.tail_until_terminal).
    """

    task_ids: set[str] = field(default_factory=set)


async def _publish_progress(
    store: PublicTaskStore, event_bus: EventBusPort, task: IngestionTask
) -> dict[str, int]:
    """Append + publish a progress event when the counts changed; return them."""

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
    return counts


async def _publish_done_once(
    store: PublicTaskStore,
    event_bus: EventBusPort,
    task: IngestionTask,
    counts: dict[str, int],
) -> None:
    """Append + publish ``.done`` unless this terminal status was already announced."""

    if task.status not in _TERMINAL_STATES:
        return
    if task.summary.get(DONE_MARKER_KEY) == task.status.value:
        return
    done_event = HarborEvent(
        name=f"task.{task.task_id}.done",
        trace_id=task.task_id,
        payload={"status": task.status.value, "counts": counts},
    )
    done_event = await store.append_task_event(task.task_id, done_event)
    await event_bus.publish(done_event)
    await store.update_summary(task.task_id, {DONE_MARKER_KEY: task.status.value})


async def _finish_vanished(
    store: PublicTaskStore, event_bus: EventBusPort, task_ids: Iterable[str]
) -> None:
    """Close out tasks that were active last tick and are no longer listed."""

    for task_id in sorted(task_ids):
        task = await store.get(task_id)
        if task is None or task.status not in _TERMINAL_STATES:
            continue  # deleted, or not terminal (e.g. finalize moved it back to a listed status)
        counts = await _publish_progress(store, event_bus, task)
        await _publish_done_once(store, event_bus, task, counts)


async def _reconcile(
    store: PublicTaskStore,
    event_bus: EventBusPort,
    tracker: ActiveTaskTracker,
    seen: set[str],
    *,
    complete: bool,
) -> None:
    """Advance the tracker; only a complete pagination may declare tasks gone."""

    if not complete:
        # An aborted tick (lost lease) never saw later pages, so their tasks
        # are not "gone" -- keep them for whichever tick completes next.
        tracker.task_ids |= seen
        return
    await _finish_vanished(store, event_bus, tracker.task_ids - seen)
    tracker.task_ids = seen


async def sync_ingestion_progress(
    store: PublicTaskStore,
    event_bus: EventBusPort,
    *,
    still_leader: Callable[[], Awaitable[bool]] | None = None,
    tracker: ActiveTaskTracker | None = None,
) -> int:
    """One poll tick: diff every active task's progress snapshot.

    For each task whose counts changed since the last tick, append + publish
    a "task.<id>.progress" event. Every task this process observes reaching
    a terminal status gets one "task.<id>.done" event -- the stream route's stop
    signal -- whether it turned terminal within this tick (re-fetched after
    its progress read) or during the sleep before it (``tracker`` remembers
    it was active). ``DONE_MARKER_KEY`` in the task summary deduplicates it per task across
    both paths and lease handoffs. Delivery is at-least-once, not strictly
    once: a crash between the event append and the marker write, or two
    overlapping leaders, can repeat it. A task no tick ever saw active
    relies on the SSE stream's terminal backstop. Returns the
    number of active tasks examined.

    list_active() is a bounded, keyset-paginated read, so this walks every
    page each tick, following the (submitted_at, task_id) cursor until a
    short page signals the end. ``still_leader``, when given, is re-checked
    before every page; if ownership is lost the tick stops early, and the
    tracker keeps (rather than finishes) the tasks it never got to see.
    """
    examined = 0
    seen: set[str] = set()
    complete = False
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
        for task in page:
            seen.add(task.task_id)
            counts = await _publish_progress(store, event_bus, task)
            refreshed = await store.get(task.task_id)
            if refreshed is not None:
                await _publish_done_once(store, event_bus, refreshed, counts)
        examined += len(page)
        if len(page) < _LIST_ACTIVE_PAGE_LIMIT:
            complete = True
            break
        after_submitted_at = page[-1].submitted_at
        after_task_id = page[-1].task_id
    if tracker is not None:
        await _reconcile(store, event_bus, tracker, seen, complete=complete)
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
    tracker = ActiveTaskTracker()
    while True:
        try:
            await sync_ingestion_progress(store, event_bus, tracker=tracker)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ingestion progress sync tick failed")
        await asyncio.sleep(interval_seconds)
