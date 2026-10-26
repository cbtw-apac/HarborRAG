"""A USD ceiling is only enforceable if real token usage can be priced."""

from __future__ import annotations

from decimal import Decimal

import pytest

from harborrag_adapters.models.runtime.provider import (
    DeploymentPricing,
    ProviderDeploymentConfig,
)

pytestmark = pytest.mark.unit


def test_cost_is_computed_from_real_input_and_output_tokens() -> None:
    pricing = DeploymentPricing(
        input_usd_per_million=Decimal("3.00"),
        output_usd_per_million=Decimal("15.00"),
    )

    # 4,000 in @ $3/M + 1,000 out @ $15/M = 0.012 + 0.015
    assert pricing.cost_for(input_tokens=4000, output_tokens=1000) == Decimal("0.027")


def test_cost_never_rounds_a_charge_down() -> None:
    pricing = DeploymentPricing(
        input_usd_per_million=Decimal("3.00"),
        output_usd_per_million=Decimal("15.00"),
    )

    # A sub-microdollar charge must not settle as free.
    assert pricing.cost_for(input_tokens=1, output_tokens=0) > Decimal(0)


def test_pricing_is_optional_so_existing_catalogs_keep_loading() -> None:
    deployment = ProviderDeploymentConfig(name="openai-primary", model="openai/gpt-x")

    assert deployment.pricing is None


def test_a_priced_deployment_exposes_its_rates() -> None:
    deployment = ProviderDeploymentConfig(
        name="openai-primary",
        model="openai/gpt-x",
        pricing={  # type: ignore[arg-type]
            "input_usd_per_million": "3.00",
            "output_usd_per_million": "15.00",
        },
    )

    assert deployment.pricing is not None
    assert deployment.pricing.output_usd_per_million == Decimal("15.00")
