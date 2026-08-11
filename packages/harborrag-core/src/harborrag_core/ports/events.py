"""EventBusPort: publish/subscribe for job- and task-progress fan-out.

Outbound only (backend -> WebSocket/SSE stream), not to be confused with
inbound webhooks. subscribe() streams events whose name starts with a given
prefix, filtered per-subscriber; publish() must never block on a slow or
stalled subscriber.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from harborrag_core.contracts.events import HarborEvent


class EventBusPort(Protocol):
    """Publish events; subscribe to a filtered, ordered stream of them."""

    async def publish(self, event: HarborEvent) -> None:
        """Deliver the event to every subscriber whose prefix matches."""

    def subscribe(self, name_prefix: str) -> AsyncIterator[HarborEvent]:
        """Stream events whose name starts with name_prefix, indefinitely."""
