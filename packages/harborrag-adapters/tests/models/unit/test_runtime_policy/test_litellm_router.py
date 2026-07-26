from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.runtime.litellm_router import build_litellm_router

from .fakes import runtime_config

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_litellm_router_disables_opaque_retry_and_forwards_budgets() -> None:
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    base = runtime_config(deployments=2).model_dump(mode="python")
    base["provider_budgets"] = {"openai": {"rpm_limit": 100}}
    config = HarborChatClientConfig.model_validate(base)

    build_litellm_router(
        config,
        config.models,
        provider_resolver=lambda _deployment: "openai",
        router_factory=factory,
    )

    assert captured["num_retries"] == 0
    assert captured["max_fallbacks"] == 0
    assert captured["cache_responses"] is False
    assert captured["provider_budget_config"]["openai"].model_dump(exclude_none=True) == {
        "rpm_limit": 100
    }
    assert [item["model_name"] for item in captured["model_list"]] == [
        "harbor::primary::primary-0",
        "harbor::primary::primary-1",
    ]
