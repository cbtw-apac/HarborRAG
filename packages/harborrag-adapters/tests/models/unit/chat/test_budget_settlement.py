from __future__ import annotations

from typing import Any

import pytest
from model_runtime_support import FakeChatInvocation, chat_config

from harborrag_adapters.models.runtime.budget import (
    BudgetExceededError,
    InMemoryBudgetPolicy,
)
from harborrag_adapters.models.runtime.distributed_config import BudgetPolicyConfig
from harborrag_core.models.chat import HarborChatMessage, StreamEventType

from .chat_client_support import sync_client

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

SCOPE = "__unscoped__:primary"


def _costed_chat(cost: float) -> dict[str, Any]:
    """Build a LiteLLM-style response whose hidden params carry ``response_cost``."""
    return {
        "id": "response",
        "model": "provider-model",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "paid"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        "_hidden_params": {"response_cost": cost},
    }


def _costed_stream(cost: float) -> list[dict[str, Any]]:
    """Build a LiteLLM-style stream whose final usage chunk carries ``response_cost``."""

    def chunk(content: str | None, finish: str | None = None) -> dict[str, Any]:
        return {
            "id": "stream-id",
            "model": "provider-model",
            "choices": [{"delta": {"content": content}, "finish_reason": finish}],
        }

    final_chunk = {
        "id": "stream-id",
        "model": "provider-model",
        "choices": [],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "_hidden_params": {"response_cost": cost},
    }
    return [chunk("hi"), chunk(None, "stop"), final_chunk]


def _daily_capped_policy(cap: float) -> InMemoryBudgetPolicy:
    return InMemoryBudgetPolicy(
        BudgetPolicyConfig(enabled=True, require_tenant_id=False, daily_cost_usd=cap)
    )


def test_chat_response_cost_settles_into_budget_and_trips_daily_cap() -> None:
    """LiteLLM's ``response_cost`` must reach the budget policy through the
    normalized response so daily spend caps are actually enforced."""
    policy = _daily_capped_policy(0.5)
    invocation = FakeChatInvocation([_costed_chat(0.4), _costed_chat(0.4), _costed_chat(0.4)])
    client = sync_client(chat_config(), backend=invocation, budget=policy)

    first = client.chat([HarborChatMessage.user("one")])
    assert first.estimated_cost_usd == 0.4
    assert policy.snapshot(SCOPE)["day_cost_usd"] == pytest.approx(0.4)

    second = client.chat([HarborChatMessage.user("two")])
    assert second.estimated_cost_usd == 0.4
    assert policy.snapshot(SCOPE)["day_cost_usd"] == pytest.approx(0.8)

    with pytest.raises(BudgetExceededError, match="daily"):
        client.chat([HarborChatMessage.user("three")])
    assert len(invocation.calls) == 2


def test_stream_response_cost_settles_into_budget_and_trips_daily_cap() -> None:
    policy = _daily_capped_policy(0.5)
    invocation = FakeChatInvocation([_costed_stream(0.3), _costed_stream(0.3), _costed_chat(0.1)])
    client = sync_client(chat_config(), backend=invocation, budget=policy)

    events = list(client.stream([HarborChatMessage.user("one")]))
    completed = events[-1]
    assert completed.event is StreamEventType.COMPLETED
    assert completed.estimated_cost_usd == 0.3
    assert policy.snapshot(SCOPE)["day_cost_usd"] == pytest.approx(0.3)

    list(client.stream([HarborChatMessage.user("two")]))
    assert policy.snapshot(SCOPE)["day_cost_usd"] == pytest.approx(0.6)

    with pytest.raises(BudgetExceededError, match="daily"):
        client.chat([HarborChatMessage.user("three")])
    assert len(invocation.calls) == 2


def test_chat_without_response_cost_leaves_budget_spend_untouched() -> None:
    policy = _daily_capped_policy(0.5)
    raw = _costed_chat(0.0)
    del raw["_hidden_params"]
    client = sync_client(chat_config(), backend=FakeChatInvocation([raw]), budget=policy)

    response = client.chat([HarborChatMessage.user("free")])
    assert response.estimated_cost_usd is None
    assert policy.snapshot(SCOPE)["day_cost_usd"] == 0.0
