"""Cost coverage distinguishes free calls from unknown or partial pricing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_core.models.cost import ModelCost


def test_unknown_cost_is_not_a_zero_dollar_estimate() -> None:
    cost = ModelCost().add_call(None)
    assert cost.amount_usd is None
    assert cost.status == "unavailable"
    assert cost.complete is False
    assert cost.model_calls == 1


def test_partial_cost_preserves_known_subtotal_and_round_trips() -> None:
    cost = ModelCost().add_call(0.02).add_call(None).add_call(0.03)
    assert cost.amount_usd == pytest.approx(0.05)
    assert cost.complete is False
    assert (cost.model_calls, cost.priced_model_calls) == (3, 2)
    assert ModelCost.model_validate(cost.model_dump(mode="json")) == cost


def test_explicit_free_call_is_complete() -> None:
    cost = ModelCost().add_call(0.0)
    assert cost.amount_usd == 0
    assert cost.complete is True
    assert cost.status == "estimated"


@pytest.mark.parametrize("amount", [-1.0, float("nan"), float("inf")])
def test_invalid_cost_is_rejected(amount: float) -> None:
    with pytest.raises(ValidationError):
        ModelCost().add_call(amount)


def test_incomplete_coverage_cannot_claim_a_complete_bill() -> None:
    with pytest.raises(ValidationError, match="complete must reflect"):
        ModelCost(complete=True, model_calls=2)
