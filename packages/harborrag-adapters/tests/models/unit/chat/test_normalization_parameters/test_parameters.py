from __future__ import annotations

import pytest
from model_runtime_support import chat_config
from pydantic import SecretStr

from harborrag_adapters.models.chat.configs import GenerationDefaults, HarborChatModelConfig
from harborrag_adapters.models.chat.parameters import (
    apply_generation_defaults,
    build_chat_request,
    build_litellm_parameters,
    chat_request_id,
    ensure_request_id,
    prepare_chat_request,
)
from harborrag_adapters.models.chat.registry import HarborProvider
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatTool,
    HarborToolCall,
    HarborToolCallFunction,
    HarborToolFunction,
    ImageURLContentPart,
    MessageRole,
    TextContentPart,
)
from harborrag_core.models.errors import (
    HarborChatConfigurationError,
    HarborChatInvalidRequestError,
)

from .conftest import deployment

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


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
