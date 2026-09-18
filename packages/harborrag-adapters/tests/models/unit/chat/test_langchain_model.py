from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.langchain import HarborChatModel

from .chat_client_support import (
    FakeAsyncStream,
    FakeInvocation,
    async_client,
    response_dict,
    stream_chunk,
)
from .langchain_model_support import Weather, make_model

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_invoke_returns_ai_message_with_usage_and_metadata(config) -> None:
    invocation = FakeInvocation([response_dict("hello there")])
    model = make_model(config, invocation)

    result = model.invoke([SystemMessage(content="Be brief"), HumanMessage(content="hi")])

    assert isinstance(result, AIMessage)
    assert result.content == "hello there"
    assert result.usage_metadata == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    assert result.response_metadata["provider"] == "openai"
    assert result.response_metadata["provider_model"] == "gpt-test"
    assert result.response_metadata["finish_reason"] == "stop"
    assert result.response_metadata["request_id"]
    call = invocation.async_calls[0]
    assert call["messages"] == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert call["user"] == "user-1"


@pytest.mark.asyncio
async def test_ainvoke_uses_async_client(config) -> None:
    invocation = FakeInvocation([response_dict("async hello")])
    model = make_model(config, invocation)

    result = await model.ainvoke("hi")

    assert result.content == "async hello"
    assert len(invocation.async_calls) == 1
    assert not invocation.calls


@pytest.mark.asyncio
async def test_astream_assembles_text_usage_and_finish_reason(config) -> None:
    raw = FakeAsyncStream(
        [
            stream_chunk("stream"),
            stream_chunk("ing "),
            stream_chunk("works"),
            stream_chunk(
                finish_reason="stop",
                usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            ),
        ]
    )
    model = make_model(config, FakeInvocation(async_streams=[raw]))

    chunks = [chunk async for chunk in model.astream("hi")]
    assembled = chunks[0]
    for chunk in chunks[1:]:
        assembled = assembled + chunk

    assert isinstance(assembled, AIMessageChunk)
    assert assembled.content == "streaming works"
    assert assembled.response_metadata["finish_reason"] == "stop"
    assert assembled.usage_metadata == {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}


@pytest.mark.asyncio
async def test_astream_assembles_tool_call_deltas(config) -> None:
    raw = FakeAsyncStream(
        [
            stream_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call-weather",
                        "type": "function",
                        "function": {"name": "Weather", "arguments": '{"city":"'},
                    }
                ]
            ),
            stream_chunk(
                tool_calls=[{"index": 0, "function": {"arguments": 'Paris"}'}}],
            ),
            stream_chunk(finish_reason="tool_calls"),
        ]
    )
    model = make_model(config, FakeInvocation(async_streams=[raw])).bind_tools([Weather])

    chunks = [chunk async for chunk in model.astream("weather?")]
    assembled = chunks[0]
    for chunk in chunks[1:]:
        assembled = assembled + chunk

    assert isinstance(assembled, AIMessageChunk)
    assert assembled.tool_calls == [
        {"name": "Weather", "args": {"city": "Paris"}, "id": "call-weather", "type": "tool_call"}
    ]


def test_supports_structured_output_is_true_when_the_deployment_declares_it(config) -> None:
    invocation = FakeInvocation()

    model = make_model(config, invocation)

    assert model.supports_structured_output is True
    # Reading a declared capability is configuration lookup, never a request.
    assert not invocation.calls
    assert not invocation.async_calls
    assert not invocation.stream_calls
    assert not invocation.async_stream_calls


def test_supports_structured_output_is_false_without_the_capability(base_config) -> None:
    """``base_config``'s deployment declares streaming and tools, not structured output."""

    model = make_model(base_config, FakeInvocation())

    assert model.supports_structured_output is False


def test_supports_structured_output_is_false_for_an_unknown_logical_model(config) -> None:
    """The hint answers ``False`` rather than raising: a wrong answer only costs a type hint."""

    model = HarborChatModel(async_client(config, backend=FakeInvocation()), logical_model="absent")

    assert model.supports_structured_output is False


def test_supports_structured_output_falls_back_to_the_catalog_default() -> None:
    """No ``logical_model`` resolves through ``default_model``, as a request would.

    Two logical models disagree about the capability, so the answer can only be
    right if the property really resolves the name a request would resolve.
    """

    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "structured",
            "models": {
                "structured": {
                    "provider": "openai",
                    "model": "openai/gpt-4o-mini",
                    "api_key": "test-key",
                    "capabilities": {"structured_output": True},
                },
                "plain": {
                    "provider": "openai",
                    "model": "openai/gpt-3.5-turbo",
                    "api_key": "test-key",
                    "capabilities": {"structured_output": False},
                },
            },
        }
    )
    client = async_client(config, backend=FakeInvocation())

    assert HarborChatModel(client).supports_structured_output is True
    assert HarborChatModel(client, logical_model="plain").supports_structured_output is False
