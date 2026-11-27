"""Progress bridge that polls active ingestion runs and fans their events out on the bus.

``progress()`` is a point-in-time snapshot, so this bridge is the diff loop that turns
snapshots into the ordered, replayable stream behind ``GET /v1/admin/ingestions/{id}/events``:
``run.status``, ``stage.progress``, ``document.failed`` and ``run.completed``.
Its snapshot and failure cursor live in the task summary. The task store keeps them when
a summary is replaced (``PROGRESS_STATE_KEYS``), or events would be emitted twice.
Stored and bus event names carry a ``task.<id>.`` routing prefix that the route strips.
The ``control`` audit events (who asked for pause, resume or cancel) are stored but never
streamed; the status change they cause reaches the stream as ``run.status``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from harborrag_core.contracts.events import HarborEvent, event_prefix
from harborrag_core.ingestion import IngestionTask, StoredTaskDocumentResult
from harborrag_core.ingestion.stages import spec_stage
from harborrag_core.observability.log_events import log_event
from harborrag_core.ports.events import EventBusPort
from harborrag_engine.control_plane.connection_views import ingestion_counters, ingestion_status
from harborrag_engine.ingestion.presenters import TERMINAL_STATES
from harborrag_runtime.ingestion.source.skips import FAILURE_CURSOR_KEY as _FAILURE_CURSOR_KEY
from harborrag_runtime.ingestion.source.skips import PROGRESS_SNAPSHOT_KEY as _SNAPSHOT_KEY

from .ports import PublicTaskStore

logger = logging.getLogger("harborrag.runtime.application.ingestion.progress_bridge")

_PUBLISH_STAGE = "publish"
_UNKNOWN_CATEGORY = "unknown"
_FAILED_PAGE_LIMIT = 200
_FAILURE_CURSOR_PARTS = 2
"""A stored failure cursor is ``[updated_at, document_id]``."""
RUN_STATUS = "run.status"
STAGE_PROGRESS = "stage.progress"
DOCUMENT_FAILED = "document.failed"
RUN_COMPLETED = "run.completed"
CONTROL_AUDIT = "control"
STREAMED_EVENTS = frozenset({RUN_STATUS, STAGE_PROGRESS, DOCUMENT_FAILED, RUN_COMPLETED})
_DEFAULT_TICK_INTERVAL_SECONDS = 2.0
_LIST_ACTIVE_PAGE_LIMIT = 500

LEASE_NAME = "ingestion_progress_bridge"
"""Lease the bridge's tick runs under (``LeaseRepositoryPort``), so one process ticks at a time.

When the API is scaled to several processes, concurrent ticks would diff the same tasks and
race their read-then-write snapshot checks into appending duplicate progress rows. The name
must match the lease seeded by control-plane migration 0017.
"""
LEASE_TTL_SECONDS = _DEFAULT_TICK_INTERVAL_SECONDS * 4
"""Lease lifetime of a few tick intervals, so a crashed leader's lease lapses quickly."""


async def sync_ingestion_progress(
    store: PublicTaskStore,
    event_bus: EventBusPort,
    *,
    still_leader: Callable[[], Awaitable[bool]] | None = None,
) -> int:
    """One poll tick: diff every active run's status, counters and failed documents.

    A run whose status or counters changed gets ``run.status``; changed counters also get
    ``stage.progress``; each newly failed document gets ``document.failed``. A run that
    reached a terminal status gets a final ``run.completed``, the stream's stop signal.

    ``list_active()`` is keyset-paginated, so this walks every page each tick. A run that
    ends between two ticks drops out of ``list_active()`` unseen; the event stream then
    completes it through ``sync_run`` when a watcher reads it.

    Args:
        store: The run registry.
        event_bus: Where each appended event is published for live tails.
        still_leader: Re-checked before every page; a lost lease stops the tick early.

    Returns:
        The number of active runs examined.
    """
    examined = 0
    after_submitted_at = None
    after_task_id = None
    while True:
        if still_leader is not None and not await still_leader():
            log_event(logger, "ingestion.progress_lease_lost", examined_count=examined)
            break
        page = await store.list_active(
            after_submitted_at=after_submitted_at,
            after_task_id=after_task_id,
            limit=_LIST_ACTIVE_PAGE_LIMIT,
        )
        if not page:
            break
        for task in page:
            await sync_run(store, event_bus, task)
        examined += len(page)
        if len(page) < _LIST_ACTIVE_PAGE_LIMIT:
            break
        after_submitted_at = page[-1].submitted_at
        after_task_id = page[-1].task_id
    return examined


async def sync_run(store: PublicTaskStore, event_bus: EventBusPort, task: IngestionTask) -> None:
    """Emit the events a run's current state owes: diffs, new failures and, once ended, ``run.completed``.

    ``run.completed`` is appended at most once per run, whichever caller gets there first.
    A run that ended before the event names changed gets only ``run.completed``; the events it
    stored stay as stored.
    """
    counts = await store.progress(task.task_id)
    refreshed = await store.get(task.task_id)
    current = refreshed if refreshed is not None else task
    terminal = current.status in TERMINAL_STATES
    counters = ingestion_counters(current, counts)
    status = ingestion_status(current.status)
    if not (terminal and _predates_explorer_events(current)):
        await _emit_changes(store, event_bus, current, counters, status, terminal=terminal)
    if terminal:
        completed = HarborEvent(
            name=f"{event_prefix(task.task_id)}{RUN_COMPLETED}",
            trace_id=task.task_id,
            payload={"counters": counters, "finish_reason": status},
        )
        stored = await store.append_task_event_once(task.task_id, completed)
        if stored is not None:
            await event_bus.publish(stored)


def _predates_explorer_events(task: IngestionTask) -> bool:
    """Whether the run's progress snapshot was written by the bridge before the event names changed."""
    snapshot = task.summary.get(_SNAPSHOT_KEY)
    return isinstance(snapshot, dict) and bool(snapshot) and "counters" not in snapshot


async def _emit_changes(  # noqa: PLR0913 - the run and the state it is compared with
    store: PublicTaskStore,
    event_bus: EventBusPort,
    task: IngestionTask,
    counters: dict[str, int],
    status: str,
    *,
    terminal: bool,
) -> None:
    """Emit ``run.status`` and ``stage.progress`` for a changed snapshot, then new failures."""
    snapshot = {"status": status, "counters": counters}
    previous = task.summary.get(_SNAPSHOT_KEY)
    previous = previous if isinstance(previous, dict) else {}
    if snapshot != previous:
        if not terminal:
            await _emit(store, event_bus, task.task_id, RUN_STATUS, snapshot)
        if counters != previous.get("counters"):
            await _emit(
                store,
                event_bus,
                task.task_id,
                STAGE_PROGRESS,
                {
                    "stage": _PUBLISH_STAGE,
                    "done": counters["processed"] + counters["skipped"] + counters["failed"],
                    "total": counters["discovered"],
                },
            )
        await store.update_summary(task.task_id, {_SNAPSHOT_KEY: snapshot})
    if counters["failed"]:
        await _emit_new_failures(store, event_bus, task)


async def _emit_new_failures(
    store: PublicTaskStore, event_bus: EventBusPort, task: IngestionTask
) -> None:
    """Emit ``document.failed`` for each failure after the stored cursor, advancing it."""
    cursor = task.summary.get(_FAILURE_CURSOR_KEY)
    after_updated_at: datetime | None = None
    after_document_id: str | None = None
    if isinstance(cursor, list) and len(cursor) == _FAILURE_CURSOR_PARTS:
        after_updated_at, after_document_id = datetime.fromisoformat(cursor[0]), cursor[1]
    while True:
        page = await store.document_results_page(
            task.task_id,
            statuses=("failed",),
            after_updated_at=after_updated_at,
            after_document_id=after_document_id,
            limit=_FAILED_PAGE_LIMIT,
        )
        for item in page.items:
            await _emit(store, event_bus, task.task_id, DOCUMENT_FAILED, _failure_payload(item))
        if not page.items:
            return
        last = page.items[-1]
        after_updated_at, after_document_id = last.updated_at, str(last.document_id)
        await store.update_summary(
            task.task_id,
            {_FAILURE_CURSOR_KEY: [after_updated_at.isoformat(), after_document_id]},
        )
        if not page.has_more:
            return


def _failure_payload(item: StoredTaskDocumentResult) -> dict[str, str]:
    """Return the ``document.failed`` payload: document, stage and error category."""
    result = item.result
    stage = result.get("failure_stage")
    code = result.get("safe_error_code")
    return {
        "document_id": str(result.get("source_item_id") or item.document_id),
        "stage": spec_stage(stage if isinstance(stage, str) else None),
        "error_category": code if isinstance(code, str) and code else _UNKNOWN_CATEGORY,
    }


async def _emit(
    store: PublicTaskStore,
    event_bus: EventBusPort,
    task_id: str,
    name: str,
    payload: dict[str, Any],
) -> None:
    event = HarborEvent(name=f"{event_prefix(task_id)}{name}", trace_id=task_id, payload=payload)
    await event_bus.publish(await store.append_task_event(task_id, event))
