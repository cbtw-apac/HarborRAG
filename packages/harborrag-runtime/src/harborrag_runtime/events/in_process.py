"""In-process EventBusPort: asyncio queues, one process, streams indefinitely.

Single-process dev/prod implementation; a Redis-backed one is future work
once the API and workers run as separate processes. Lives in harborrag_runtime
(not harborrag_adapters) because it has no I/O dependency of its own -- it's a
process-local singleton, not a repository over external storage.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncGenerator

from harborrag_core.contracts.events import HarborEvent

logger = logging.getLogger("harborrag.runtime.events.in_process")

_Subscription = tuple[str, "asyncio.Queue[HarborEvent]"]

# A subscriber that never reads (a stalled/leaked consumer) must not retain
# every event forever. Progress snapshots are safe to coalesce -- a later one
# supersedes an earlier one -- so a full queue drops its oldest entry rather
# than blocking publish() or growing unbounded. A terminal ".done" event is
# never dropped: by construction it's always the last event published for a
# given subscription, so it only ever evicts an older progress entry, never
# itself.
_DEFAULT_MAX_QUEUE_SIZE = 256


class InProcessEventBus:
    """EventBusPort over per-subscriber asyncio queues, filtered by name prefix."""

    def __init__(self, max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._max_queue_size = max_queue_size
        self._subscribers: list[_Subscription] = []

    async def publish(self, event: HarborEvent) -> None:
        """Deliver the event to every subscriber whose prefix matches.

        Never blocks: a full queue drops its oldest entry to make room
        rather than making publish() wait on a slow consumer.
        """
        for prefix, queue in list(self._subscribers):
            if not event.name.startswith(prefix):
                continue
            while True:
                try:
                    queue.put_nowait(event)
                    break
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        continue
                    logger.warning(
                        "Subscriber queue full for prefix=%r; dropped the oldest event "
                        "to admit event.name=%r",
                        prefix,
                        event.name,
                    )

    def subscribe(self, name_prefix: str) -> AsyncGenerator[HarborEvent, None]:
        """Stream events whose name starts with name_prefix, indefinitely.

        Registration happens synchronously, here, before returning -- not
        inside the async generator below, which wouldn't run any code until
        first iterated. That laziness would otherwise open a gap: a caller
        that reads a DB backlog and only *then* starts consuming this stream
        must be guaranteed that anything published in between is captured
        the moment subscribe() is called, not the moment iteration begins.

        The flip side of that same laziness: if the returned iterator is
        abandoned before it's ever iterated -- e.g. a caller subscribes,
        then gets cancelled while still reading a backlog -- the async
        generator's frame never starts, so its try/finally below never
        runs and closing/GC'ing it would leave the subscription registered
        forever. A weakref.finalize tied to the generator object itself
        (not to its execution reaching the finally) closes that gap: it
        fires on GC regardless of whether the generator ever started.
        """
        queue: asyncio.Queue[HarborEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        subscription = (name_prefix, queue)
        self._subscribers.append(subscription)
        agen = self._consume(subscription)
        weakref.finalize(agen, self._discard, subscription)
        return agen

    def _discard(self, subscription: _Subscription) -> None:
        try:
            self._subscribers.remove(subscription)
        except ValueError:
            pass

    async def _consume(self, subscription: _Subscription) -> AsyncGenerator[HarborEvent, None]:
        _, queue = subscription
        try:
            while True:
                yield await queue.get()
        finally:
            self._discard(subscription)
