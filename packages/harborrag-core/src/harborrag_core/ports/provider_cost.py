"""ProviderCostTrackerPort: a live, process-lifetime spend counter per provider.

Deliberately not a database-backed repository: ML4-P2 scopes provider cost as
an in-memory running total that resets on restart, not permanent history --
see the ``/v1/providers/cost`` ticket note. Kept as its own port (not folded
into ``control_plane.py``'s repository protocols) because nothing here is
persisted and there is no tenant-scoped CRUD shape to match.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Protocol


class ProviderCostTrackerPort(Protocol):
    """Accumulate and report spend per provider id since process start."""

    started_at: datetime

    def record(self, provider_id: str, cost_usd: float) -> None:
        """Add ``cost_usd`` to ``provider_id``'s running total for this process."""

    def snapshot(self, provider_ids: Iterable[str]) -> Mapping[str, float]:
        """Current total spend for each id in ``provider_ids`` (0.0 if never recorded)."""
