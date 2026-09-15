"""A run stops cleanly at its USD ceiling instead of failing or overspending."""

from __future__ import annotations

from decimal import Decimal

import pytest

from harborrag_runtime.topology.budgeted_extractor import EnrichmentDeferredError
from harborrag_runtime.topology.run_cost import RunCostLedger

pytestmark = pytest.mark.unit


def test_an_unset_ceiling_never_blocks() -> None:
    ledger = RunCostLedger(ceiling_usd=None)
    ledger.record(Decimal("1000"))

    ledger.ensure_capacity()  # does not raise


def test_spend_below_the_ceiling_keeps_the_run_going() -> None:
    ledger = RunCostLedger(ceiling_usd=Decimal("5.00"))
    ledger.record(Decimal("4.99"))

    ledger.ensure_capacity()


def test_reaching_the_ceiling_defers_rather_than_fails() -> None:
    ledger = RunCostLedger(ceiling_usd=Decimal("5.00"))
    ledger.record(Decimal("5.00"))

    # EnrichmentDeferredError is what the coordinator maps to "deferred:<reason>";
    # any other exception would be recorded as an unexplained failure instead.
    with pytest.raises(EnrichmentDeferredError, match="run_budget_exhausted"):
        ledger.ensure_capacity()


def test_unpriced_calls_do_not_silently_consume_the_ceiling() -> None:
    # A deployment without pricing reports no cost; counting it as zero would let an
    # unpriced model run past the ceiling forever.
    ledger = RunCostLedger(ceiling_usd=Decimal("5.00"))
    ledger.record(None)

    assert ledger.unpriced_calls == 1
    assert ledger.spent_usd == Decimal(0)


def test_the_ledger_reports_what_it_spent() -> None:
    ledger = RunCostLedger(ceiling_usd=Decimal("5.00"))
    ledger.record(Decimal("1.25"))
    ledger.record(Decimal("0.75"))

    assert ledger.spent_usd == Decimal("2.00")
