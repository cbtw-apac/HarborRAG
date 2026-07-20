"""Event bus port (plan §4.2): HarborEvent pub/sub between runtime and API.

In-process implementation for single-process dev; Redis-backed when API and
workers run as separate processes (M2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from harborrag_core.contracts.events import HarborEvent


class EventBusPort(Protocol):
    """Publish HarborEvents and subscribe to name-prefixed streams."""

    async def publish(self, event: HarborEvent) -> None:
        """Deliver an event to all current subscribers of matching prefixes."""

    def subscribe(self, name_prefix: str) -> AsyncIterator[HarborEvent]:
        """Async stream of events whose name starts with name_prefix."""
        ...
