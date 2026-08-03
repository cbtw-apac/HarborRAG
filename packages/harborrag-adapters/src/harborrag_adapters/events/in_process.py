"""In-process EventBusPort: asyncio queues, one process, streams indefinitely.

Matches the port's own docstring -- this is the single-process dev/prod
implementation; a Redis-backed one is future work once the API and workers
run as separate processes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from harborrag_core.contracts.events import HarborEvent

_Subscription = tuple[str, "asyncio.Queue[HarborEvent]"]


class InProcessEventBus:
    """EventBusPort over per-subscriber asyncio queues, filtered by name prefix."""

    def __init__(self) -> None:
        self._subscribers: list[_Subscription] = []

    async def publish(self, event: HarborEvent) -> None:
        """Deliver the event to every subscriber whose prefix matches."""
        for prefix, queue in list(self._subscribers):
            if event.name.startswith(prefix):
                await queue.put(event)

    def subscribe(self, name_prefix: str) -> AsyncIterator[HarborEvent]:
        """Stream events whose name starts with name_prefix, indefinitely.

        Registration happens synchronously, here, before returning -- not
        inside the async generator below, which wouldn't run any code until
        first iterated. That laziness would otherwise open a gap: a caller
        that reads a DB backlog and only *then* starts consuming this stream
        must be guaranteed that anything published in between is captured
        the moment subscribe() is called, not the moment iteration begins.
        """
        queue: asyncio.Queue[HarborEvent] = asyncio.Queue()
        subscription = (name_prefix, queue)
        self._subscribers.append(subscription)
        return self._consume(subscription)

    async def _consume(self, subscription: _Subscription) -> AsyncIterator[HarborEvent]:
        _, queue = subscription
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.remove(subscription)
