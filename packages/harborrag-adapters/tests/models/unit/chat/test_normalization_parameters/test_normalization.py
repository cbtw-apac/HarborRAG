from __future__ import annotations

import pytest

from harborrag_adapters.models.chat.normalization import (
    normalize_chat_response,
    normalize_chat_usage,
    normalize_finish_reason,
    normalize_tool_call_delta,
    normalize_tool_calls,
    parse_tool_arguments,
)
from harborrag_core.models.errors import HarborChatProviderError

from .conftest import deployment

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_normalize_chat_response_with_usage_tools_and_metadata() -> None:
    raw = {
        "id": "resp-1",
        "created": "12",
        "model": "provider-model",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id":1}'},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 1},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
        "_hidden_params": {
            "additional_headers": {"x-request-id": "provider-1"},
            "cache_hit": True,
            "response_cost": 0.01,
        },
    }
    response = normalize_chat_response(
        raw,
        logical_model="primary",
        deployment=deployment(),
        request_id="req-1",
        latency_ms=12.5,
    )
    assert response.id == "resp-1"
    assert response.provider_model == "provider-model"
    assert response.finish_reason.value == "tool_calls"
    assert response.usage.total_tokens == 5
    assert response.usage.cache_read_input_tokens == 1
    assert response.tool_calls[0].function.parsed_arguments == {"id": 1}
    assert response.provider_request_id == "provider-1"
    assert response.cache_hit is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "unknown"),
        ("eos", "stop"),
        ("max_tokens", "length"),
        ("safety", "content_filter"),
        ("other", "unknown"),
    ],
)
def test_normalize_finish_reason_aliases(value: object, expected: str) -> None:
    assert normalize_finish_reason(value).value == expected


def test_normalize_usage_supports_input_output_fields_and_rejects_invalid_counts() -> None:
    usage = normalize_chat_usage(
        {"input_tokens": 4, "output_tokens": 5, "cache_creation_input_tokens": 2}
    )
    assert usage.total_tokens == 9
    assert usage.cache_creation_input_tokens == 2
    for bad in (-1, True, "bad"):
        with pytest.raises(HarborChatProviderError):
            normalize_chat_usage({"prompt_tokens": bad})


def test_tool_call_normalization_complete_legacy_and_delta() -> None:
    assert normalize_tool_calls(None) == ()
    legacy = normalize_tool_calls(None, legacy_function_call={"name": "old", "arguments": {"x": 1}})
    assert legacy[0].function.parsed_arguments == {"x": 1}
    delta = normalize_tool_call_delta(
        {"id": "c", "function": {"name": "fn", "arguments": "{"}}, fallback_index=2
    )
    assert delta.index == 2
    assert delta.function.parsed_arguments is None
    assert parse_tool_arguments('{"ok":true}') == {"ok": True}
    assert parse_tool_arguments("[]") is None
    assert parse_tool_arguments("bad") is None
    with pytest.raises(HarborChatProviderError):
        normalize_tool_calls("bad")
    with pytest.raises(HarborChatProviderError):
        normalize_tool_calls([{"id": "x", "function": {}}])
    with pytest.raises(HarborChatProviderError):
        normalize_tool_call_delta(None, fallback_index=0)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"role": "user", "content": "x"}}]},
        {"choices": [{"message": {"role": "assistant", "content": ["x"]}}]},
    ],
)
def test_malformed_chat_responses_raise_stable_provider_error(raw: object) -> None:
    with pytest.raises(HarborChatProviderError):
        normalize_chat_response(
            raw,
            logical_model="primary",
            deployment=deployment(),
            request_id="req",
            latency_ms=1,
        )
