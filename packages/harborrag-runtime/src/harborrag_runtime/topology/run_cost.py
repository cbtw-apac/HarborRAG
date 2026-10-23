"""Run-scoped spend ceiling for the parent rollup.

The tenant ledger is per-day and the parent call budget is per-document, so neither
bounds a single operator-initiated run. This does, and it stops the run by *deferring*:
work already frozen stays frozen and is reused, and the rest is picked up by the next
sweep rather than being recorded as a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .budgeted_extractor import EnrichmentDeferredError


@dataclass
class RunCostLedger:
    """Accumulate measured spend for one run and refuse to start work past the ceiling."""

    ceiling_usd: Decimal | None
    spent_usd: Decimal = Decimal(0)
    # Calls whose deployment declares no pricing. Counted, never priced as zero: doing so
    # would let an unpriced model run past the ceiling indefinitely.
    unpriced_calls: int = field(default=0)

    def record(self, cost_usd: Decimal | None) -> None:
        """Account for one completed call."""

        if cost_usd is None:
            self.unpriced_calls += 1
            return
        if cost_usd < 0:
            raise ValueError("a recorded call cost cannot be negative")
        self.spent_usd += cost_usd

    def ensure_capacity(self) -> None:
        """Defer the run when the next call would spend beyond the ceiling."""

        if self.ceiling_usd is None:
            return
        if self.spent_usd >= self.ceiling_usd:
            raise EnrichmentDeferredError("run_budget_exhausted")
