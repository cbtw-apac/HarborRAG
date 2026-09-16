"""ProviderCostTrackerPort implementation: an in-memory, process-lifetime spend counter.

Mirrors ``InMemoryBudgetPolicy`` (same module) in spirit: a thread-safe
in-process counter with no persistence, since the ``/v1/providers/cost``
snapshot is explicitly a live "since the last app restart" view rather than
permanent history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from threading import RLock

from harborrag_core.base import utc_now


class InMemoryProviderCostTracker:
    """Accumulate and report spend per provider id since this process started."""

    def __init__(self) -> None:
        """Start the running totals empty, timestamped at process/tracker start."""
        self.started_at: datetime = utc_now()
        self._totals: dict[str, float] = {}
        self._lock = RLock()

    def record(self, provider_id: str, cost_usd: float) -> None:
        """Add ``cost_usd`` to ``provider_id``'s running total."""
        with self._lock:
            self._totals[provider_id] = self._totals.get(provider_id, 0.0) + cost_usd

    def snapshot(self, provider_ids: Iterable[str]) -> Mapping[str, float]:
        """Current total spend for each id in ``provider_ids`` (0.0 if never recorded)."""
        with self._lock:
            return {provider_id: self._totals.get(provider_id, 0.0) for provider_id in provider_ids}
