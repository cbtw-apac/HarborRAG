"""Resolve response pricing through LiteLLM without guessing missing usage."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from harborrag_adapters.models.runtime.responses import (
    coerce_sdk_mapping,
    sdk_hidden_parameters,
)

from .configs import HarborChatProviderConfig
from .registry import ProviderRegistry


def normalize_response_cost(hidden: Mapping[str, Any]) -> float | None:
    """Accept a finite, nonnegative LiteLLM ``response_cost`` when available."""

    cost = hidden.get("response_cost")
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost) if math.isfinite(cost) and cost >= 0 else None


def response_cost(raw: Any, *, deployment: HarborChatProviderConfig) -> float | None:
    """Use LiteLLM's attached cost or its calculator over reported token usage.

    LiteLLM owns pricing, including cached-token and provider-specific rules.
    Missing usage or unsupported model prices remain unknown. Pricing failures
    cannot invalidate an otherwise successful model response.
    """

    data = coerce_sdk_mapping(raw)
    cost = normalize_response_cost(sdk_hidden_parameters(raw, data))
    if cost is not None:
        return cost
    usage = coerce_sdk_mapping(data.get("usage"))
    if not usage or not any(
        key in usage
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens")
    ):
        return None
    try:
        from litellm import completion_cost

        provider = (
            deployment.custom_llm_provider
            or ProviderRegistry.default().get(deployment.provider).litellm_provider
        )
        amount = completion_cost(
            completion_response=raw,
            model=str(data.get("model") or deployment.model),
            custom_llm_provider=provider,
        )
    except Exception:  # noqa: BLE001 - missing pricing must not fail a paid-for response
        return None
    return normalize_response_cost({"response_cost": amount})


__all__ = ["normalize_response_cost", "response_cost"]
