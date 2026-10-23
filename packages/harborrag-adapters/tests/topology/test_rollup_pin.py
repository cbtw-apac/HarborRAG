"""The rollup picks its own model, without inheriting the extraction prompt contract."""

from __future__ import annotations

from decimal import Decimal

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.topology.descriptions import pin_rollup_model

pytestmark = pytest.mark.unit


def _catalog() -> HarborChatClientConfig:
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/extractor-v1",
                    "api_key": "not-a-real-key",
                    "capabilities": {"structured_output": True},
                },
                "summariser": {
                    "provider": "openai",
                    "model": "openai/cheap-summariser",
                    "api_key": "not-a-real-key",
                    "capabilities": {"structured_output": True},
                    "pricing": {
                        "input_usd_per_million": "0.15",
                        "output_usd_per_million": "0.60",
                    },
                },
            },
        }
    )


def test_a_named_model_is_pinned_without_touching_the_extraction_profile() -> None:
    pinned, _ = pin_rollup_model(_catalog(), "summariser")

    assert set(pinned.models) == {"summariser"}
    assert pinned.default_model == "summariser"


def test_pinning_disables_failover_so_reserved_call_counts_stay_upper_bounds() -> None:
    pinned, _ = pin_rollup_model(_catalog(), "summariser")

    assert pinned.retry.same_deployment_attempts == 1
    assert pinned.retry.max_deployment_failovers == 0
    assert pinned.retry.max_model_fallbacks == 0


def test_declared_pricing_is_surfaced_so_spend_can_be_measured() -> None:
    _, pricing = pin_rollup_model(_catalog(), "summariser")

    assert pricing is not None
    assert pricing.output_usd_per_million == Decimal("0.60")


def test_an_unpriced_model_reports_no_pricing_rather_than_guessing() -> None:
    _, pricing = pin_rollup_model(_catalog(), "primary")

    assert pricing is None


def test_an_unknown_model_is_rejected() -> None:
    # Matches the catalog's existing lookup failure rather than wrapping it.
    with pytest.raises(KeyError, match="unknown logical model"):
        pin_rollup_model(_catalog(), "nonexistent")
