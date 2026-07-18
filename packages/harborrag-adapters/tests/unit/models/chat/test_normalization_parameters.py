from __future__ import annotations

from typing import Any

import pytest
from harborrag_adapters.models.chat.configs import (
    GenerationDefaults,
    HarborChatModelConfig,
    HarborChatProviderConfig,
)
from harborrag_adapters.models.chat.normalization import (
    normalize_chat_response,
    normalize_chat_usage,
    normalize_finish_reason,
    normalize_tool_call_delta,
    normalize_tool_calls,
    parse_tool_arguments,
)
from harborrag_adapters.models.chat.parameters import (
    apply_generation_defaults,
    build_chat_request,
    build_litellm_parameters,
    chat_request_id,
    ensure_request_id,
    prepare_chat_request,
)
from harborrag_adapters.models.chat.registry import HarborProvider
from harborrag_adapters.models.chat.validation import validate_chat_request
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatTool,
    HarborTokenBudget,
    HarborToolCall,
    HarborToolCallFunction,
    HarborToolFunction,
    ImageURLContentPart,
    InputAudioContentPart,
    MessageRole,
    TextContentPart,
)
from harborrag_core.models.errors import (
    HarborChatCapabilityError,
    HarborChatConfigurationError,
    HarborChatInvalidRequestError,
    HarborChatProviderError,
)
from model_runtime_support import chat_config
from pydantic import BaseModel, SecretStr


class Answer(BaseModel):
    """Represent a minimal structured response schema."""

    answer: str


def deployment(**updates: Any) -> HarborChatProviderConfig:
    """Build a feature-rich OpenAI deployment for direct unit tests."""
    values: dict[str, Any] = {
        "name": "chat-a",
        "provider": HarborProvider.OPENAI,
        "model": "openai/gpt-test",
        "api_key": "secret",
        "headers": {"X-Deploy": SecretStr("one")},
        "capabilities": HarborChatCapabilities(
            multimodal=True,
            audio_input=True,
            structured_output=True,
            json_mode=True,
            tools=True,
            streaming=True,
        ),
    }
    values.update(updates)
    return HarborChatProviderConfig(**values)


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


def test_build_chat_request_exclusivity_and_model_override() -> None:
    request = HarborChatRequest(messages=(HarborChatMessage.user("hello"),), logical_model="a")
    assert build_chat_request(None, request=request, model=None, request_kwargs={}) is request
    overridden = build_chat_request(None, request=request, model="b", request_kwargs={})
    assert overridden.logical_model == "b"
    built = build_chat_request(
        [{"role": "user", "content": "hello"}],
        request=None,
        model="primary",
        request_kwargs={"temperature": 0.2},
    )
    assert built.messages[0].content == "hello"
    with pytest.raises(HarborChatInvalidRequestError):
        build_chat_request(
            [HarborChatMessage.user("x")],
            request=request,
            model=None,
            request_kwargs={},
        )
    with pytest.raises(HarborChatInvalidRequestError):
        build_chat_request(None, request=request, model=None, request_kwargs={"temperature": 1})
    with pytest.raises(HarborChatInvalidRequestError):
        build_chat_request(None, request=None, model=None, request_kwargs={})


def test_defaults_request_identity_and_prepare_errors() -> None:
    logical = HarborChatModelConfig(
        deployments=(deployment(),),
        default_params=GenerationDefaults(temperature=0.3, max_tokens=10),
    )
    request = HarborChatRequest(messages=(HarborChatMessage.user("x"),), temperature=0.1)
    defaulted = apply_generation_defaults(request, "primary", logical)
    assert defaulted.temperature == 0.1 and defaulted.max_tokens == 10
    identified = ensure_request_id(defaulted)
    assert chat_request_id(identified)
    assert ensure_request_id(identified) is identified
    with pytest.raises(RuntimeError):
        chat_request_id(request)
    config = chat_config()
    logical_name, selected, prepared = prepare_chat_request(
        config,
        [HarborChatMessage.user("hello")],
        request=None,
        model=None,
        request_kwargs={},
    )
    assert logical_name == "primary" and selected.name == "openai-a"
    assert prepared.metadata.request_id
    with pytest.raises(HarborChatConfigurationError):
        prepare_chat_request(
            config,
            [HarborChatMessage.user("x")],
            request=None,
            model="missing",
            request_kwargs={},
        )


def test_litellm_parameter_rendering_merges_headers_and_options() -> None:
    tool_call = HarborToolCall(
        id="call",
        function=HarborToolCallFunction(name="lookup", arguments='{"x":1}'),
    )
    messages = (
        HarborChatMessage.system("system"),
        HarborChatMessage(
            role=MessageRole.USER,
            content=(
                TextContentPart(text="text"),
                ImageURLContentPart(image_url={"url": "https://example.test/image.png"}),
            ),
            name="named",
        ),
        HarborChatMessage.assistant(None, tool_calls=(tool_call,)),
        HarborChatMessage(role=MessageRole.TOOL, content="result", tool_call_id="call"),
    )
    tool = HarborChatTool(function=HarborToolFunction(name="lookup", parameters={"type": "object"}))
    request = HarborChatRequest(
        messages=messages,
        logical_model="primary",
        metadata={"request_id": "r", "user_id": "u"},
        temperature=0.2,
        stop=("a", "b"),
        tools=(tool,),
        tool_choice="auto",
        parallel_tool_calls=True,
        response_format={"type": "json_object"},
        custom_headers={"X-Request": SecretStr("two")},
        extra_params={"frequency_penalty": 0.1},
    )
    params = build_litellm_parameters(deployment(), request, timeout=5, stream=True)
    assert params["model"] == "openai/gpt-test"
    assert params["extra_headers"] == {"X-Deploy": "one", "X-Request": "two"}
    assert params["stream_options"] == {"include_usage": True}
    assert params["stop"] == ["a", "b"]
    assert params["messages"][1]["content"][1]["type"] == "image_url"
    assert params["messages"][2]["tool_calls"][0]["function"]["name"] == "lookup"
    assert params["frequency_penalty"] == 0.1
    with pytest.raises(HarborChatInvalidRequestError):
        build_litellm_parameters(
            deployment(),
            request.model_copy(update={"extra_params": {"model": "bad"}}),
            timeout=1,
        )


def test_azure_parameter_model_uses_deployment_name() -> None:
    azure = deployment(
        provider=HarborProvider.AZURE_OPENAI,
        model="azure/gpt-logical",
        deployment_name="production",
        api_base="https://example.openai.azure.com",
        api_version="2025-01-01",
    )
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("x"),),
        logical_model="primary",
        metadata={"request_id": "r"},
    )
    assert build_litellm_parameters(azure, request, timeout=1)["model"] == "azure/production"


def test_chat_request_capability_and_security_validation() -> None:
    base = deployment(capabilities=HarborChatCapabilities(streaming=True))
    config = chat_config(deployments=(base,))
    requests = [
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), reasoning_effort="high"),
        HarborChatRequest(
            messages=(HarborChatMessage.user("x"),),
            token_budget=HarborTokenBudget(max_input_tokens=10),
        ),
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), tool_choice="auto"),
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), parallel_tool_calls=True),
        HarborChatRequest(messages=(HarborChatMessage(role=MessageRole.TOOL, content="x"),)),
        HarborChatRequest(
            messages=(HarborChatMessage.user("x"),),
            response_format={"type": "json_object"},
        ),
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), extra_params={"model": "bad"}),
        HarborChatRequest(
            messages=(HarborChatMessage.user("x"),),
            custom_headers={"Authorization": "bad"},
        ),
    ]
    for request in requests:
        with pytest.raises((HarborChatCapabilityError, HarborChatInvalidRequestError)):
            validate_chat_request(request, config, base)
    multimodal = HarborChatRequest(
        messages=(
            HarborChatMessage(
                role=MessageRole.USER,
                content=(ImageURLContentPart(image_url={"url": "https://example.test/image.png"}),),
            ),
        )
    )
    with pytest.raises(HarborChatCapabilityError):
        validate_chat_request(multimodal, config, base)
    audio_only = deployment(capabilities=HarborChatCapabilities(multimodal=True, audio_input=False))
    audio_request = HarborChatRequest(
        messages=(
            HarborChatMessage(
                role=MessageRole.USER,
                content=(InputAudioContentPart(input_audio={"data": "eA==", "format": "wav"}),),
            ),
        )
    )
    with pytest.raises(HarborChatCapabilityError):
        validate_chat_request(audio_request, chat_config(deployments=(audio_only,)), audio_only)
