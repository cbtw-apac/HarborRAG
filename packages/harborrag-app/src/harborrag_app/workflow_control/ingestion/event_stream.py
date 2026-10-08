"""Replay a task's persisted events, then tail it live until it is terminal.

Two failure modes this module exists to close:

* A long-running task can persist far more events than one store page, so the
  backlog is walked page by page until a short page.
* The live tail used to wait on the event bus for a ".done" forever. The
  progress bridge can miss one (a lease handoff resets its in-process
  tracker; the bridge may not be running at all), so the tail also polls the
  task's durable status and closes the stream itself once it is terminal.

The tail never cancels the bus subscription's pending ``__anext__`` on a poll
timeout: cancelling it throws into the subscription's generator, whose
``finally`` deregisters it, silently ending the live feed. One pending future
is kept across polls and only cancelled when the tail itself ends.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator

from harborrag_core.contracts.events import HarborEvent

from .ports import PublicTaskStore
from .presenters import TERMINAL_STATES

BACKLOG_PAGE_LIMIT = 500
STATUS_POLL_SECONDS = 5.0


def _is_done(event: HarborEvent) -> bool:
    return event.name.endswith(".done")


async def replay_backlog(
    store: PublicTaskStore, task_id: str, *, after_seq: int | None
) -> AsyncGenerator[HarborEvent, None]:
    """Yield every persisted event after ``after_seq``, one bounded page at a time."""

    cursor = after_seq
    while True:
        page = await store.list_task_events(task_id, after_seq=cursor, limit=BACKLOG_PAGE_LIMIT)
        for event in page:
            yield event
        if len(page) < BACKLOG_PAGE_LIMIT or page[-1].seq is None:
            return
        cursor = page[-1].seq


async def _next_live(live: AsyncIterator[HarborEvent]) -> HarborEvent | None:
    try:
        return await anext(live)
    except StopAsyncIteration:
        return None


async def _closing_events(
    store: PublicTaskStore, task_id: str, *, last_seq: int | None
) -> list[HarborEvent] | None:
    """The events that close a terminal task's stream, or ``None`` while it runs.

    Persisted events after ``last_seq`` come first (a ".done" may have been
    appended after the live feed missed it). Only when none of them is a
    ".done" is one synthesized from the durable status; it carries no
    ``seq`` because it was never persisted.
    """

    task = await store.get(task_id)
    if task is not None and task.status not in TERMINAL_STATES:
        return None
    closing: list[HarborEvent] = []
    async with contextlib.aclosing(replay_backlog(store, task_id, after_seq=last_seq)) as backlog:
        async for event in backlog:
            closing.append(event)
            if _is_done(event):
                return closing
    if task is None:
        return closing
    counts = await store.progress(task_id)
    closing.append(
        HarborEvent(
            name=f"task.{task_id}.done",
            trace_id=task_id,
            payload={"status": task.status.value, "counts": counts},
        )
    )
    return closing


async def tail_until_terminal(
    store: PublicTaskStore,
    task_id: str,
    live: AsyncIterator[HarborEvent],
    *,
    last_seq: int | None,
) -> AsyncGenerator[HarborEvent, None]:
    """Yield live events until a ".done", polling task status as the backstop."""

    pending: asyncio.Future[HarborEvent | None] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(_next_live(live))
            finished, _ = await asyncio.wait({pending}, timeout=STATUS_POLL_SECONDS)
            if pending in finished:
                event = pending.result()
                pending = None
                if event is None:
                    return
                last_seq = event.seq if event.seq is not None else last_seq
                yield event
                if _is_done(event):
                    return
                continue
            closing = await _closing_events(store, task_id, last_seq=last_seq)
            if closing is None:
                continue
            for event in closing:
                yield event
            return
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending


__all__ = [
    "BACKLOG_PAGE_LIMIT",
    "STATUS_POLL_SECONDS",
    "replay_backlog",
    "tail_until_terminal",
]
