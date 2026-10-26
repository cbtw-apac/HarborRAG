"""Response and stream cost use LiteLLM's pricing and actual provider usage."""

from __future__ import annotations

from unittest.mock import Mock

import litellm
import pytest

from harborrag_adapters.models.chat.configs import HarborChatProviderConfig
from harborrag_adapters.models.chat.cost import response_cost
from harborrag_adapters.models.chat.normalization import normalize_chat_response
from harborrag_adapters.models.chat.streaming import ChatStreamNormalizer

pytestmark = pytest.mark.unit


def _deployment() -> HarborChatProviderConfig:
    return HarborChatProviderConfig(
        name="primary", provider="openai", model="gpt-4o-mini", api_key="test"
    )


def _response() -> dict:
    return {
        "id": "response-1",
        "model": "gpt-4o-mini",
        "choices": [
            {"message": {"role": "assistant", "content": "Answer"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 50},
        },
    }


def test_attached_litellm_cost_takes_precedence(monkeypatch) -> None:
    calculate = Mock(side_effect=AssertionError("already priced"))
    monkeypatch.setattr(litellm, "completion_cost", calculate)
    raw = {**_response(), "_hidden_params": {"response_cost": 0.004}}
    assert response_cost(raw, deployment=_deployment()) == 0.004
    calculate.assert_not_called()


def test_response_falls_back_to_litellm_with_original_usage(monkeypatch) -> None:
    calculate = Mock(return_value=0.003)
    monkeypatch.setattr(litellm, "completion_cost", calculate)
    raw = _response()
    result = normalize_chat_response(
        raw, deployment=_deployment(), logical_model="primary", request_id="req", latency_ms=1
    )
    assert result.estimated_cost_usd == 0.003
    calculate.assert_called_once_with(
        completion_response=raw, model="gpt-4o-mini", custom_llm_provider="openai"
    )


def test_stream_calculates_once_from_final_usage_including_cache_details(monkeypatch) -> None:
    calculate = Mock(return_value=0.002)
    monkeypatch.setattr(litellm, "completion_cost", calculate)
    normalizer = ChatStreamNormalizer(
        deployment=_deployment(), logical_model="primary", request_id="req"
    )
    normalizer.consume({"model": "gpt-4o-mini", "choices": [{"delta": {"content": "Answer"}}]})
    normalizer.consume({**_response(), "choices": []})
    calculate.assert_not_called()
    assert normalizer.complete().estimated_cost_usd == 0.002
    assert normalizer.complete().estimated_cost_usd == 0.002
    calculate.assert_called_once()
    assert calculate.call_args.kwargs["completion_response"]["usage"] == _response()["usage"]


def test_missing_usage_stays_unknown(monkeypatch) -> None:
    calculate = Mock(side_effect=AssertionError("no usage to price"))
    monkeypatch.setattr(litellm, "completion_cost", calculate)
    assert response_cost({"model": "gpt-4o-mini"}, deployment=_deployment()) is None
    calculate.assert_not_called()


def test_unsupported_prices_do_not_fail_response(monkeypatch) -> None:
    monkeypatch.setattr(litellm, "completion_cost", Mock(side_effect=ValueError("unknown model")))
    assert response_cost(_response(), deployment=_deployment()) is None


def test_installed_litellm_prices_provider_reported_usage() -> None:
    amount = response_cost(_response(), deployment=_deployment())
    assert amount is not None
    assert amount > 0
