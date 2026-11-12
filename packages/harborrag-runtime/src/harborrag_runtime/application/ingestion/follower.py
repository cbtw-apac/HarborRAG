"""Follow one ingestion run's events for a tenant.

Handles:
- The tenant guard on a run's event stream, which the event iterator itself does not carry
- A run that has ended with nothing to send after the resume point, as no stream

Does NOT frame events as SSE; the transport does.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol

from harborrag_core.contracts.events import HarborEvent
from harborrag_engine.control_plane.connection_views import ingestion_status
from harborrag_engine.ingestion.operations import IngestionOperations
from harborrag_engine.ingestion.presenters import TERMINAL_STATES

_ENDED_STATUSES = frozenset(ingestion_status(state) for state in TERMINAL_STATES)


class IngestionEventStream(Protocol):
    """Backlog replay then a live tail of one run's events."""

    def __call__(
        self, task_id: str, *, after_seq: int | None = None
    ) -> AsyncGenerator[HarborEvent, None]:
        """Yield the run's events after ``after_seq`` until ``run.completed``."""


class IngestionFollower:
    """Follows a tenant's run events once the run is known to belong to the tenant.

    Args:
        runs: Run reads, which are the tenant guard.
        events: The event stream of a run, which knows no tenant.
    """

    def __init__(self, *, runs: IngestionOperations, events: IngestionEventStream) -> None:
        self._runs = runs
        self._events = events

    async def follow(
        self, *, tenant_id: str, ingestion_id: str, after_seq: int | None
    ) -> AsyncGenerator[HarborEvent, None] | None:
        """Follow a run's stored events, then its live ones, until ``run.completed``.

        The run is read first, which is the only tenant guard the event iterator has. A run
        that has ended is read for its first event before returning, so its wait is bounded.

        Args:
            tenant_id: Tenant the run must belong to.
            ingestion_id: The run to follow.
            after_seq: Resume after this event sequence; ``None`` starts at the first.

        Returns:
            The events, or ``None`` when the run has ended and nothing follows ``after_seq``.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
        """
        run = await self._runs.get(tenant_id=tenant_id, ingestion_id=ingestion_id)
        events = self._events(ingestion_id, after_seq=after_seq)
        if run["status"] not in _ENDED_STATUSES:
            return events
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            return None
        return _after_first(first, events)


async def _after_first(
    first: HarborEvent, rest: AsyncGenerator[HarborEvent, None]
) -> AsyncGenerator[HarborEvent, None]:
    """Yield ``first``, then ``rest``, closing ``rest`` when the caller stops."""
    try:
        yield first
        async for event in rest:
            yield event
    finally:
        await rest.aclose()
