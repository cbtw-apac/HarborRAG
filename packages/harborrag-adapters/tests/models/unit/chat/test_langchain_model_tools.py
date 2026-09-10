"""HarborChatModel tool binding, structured output, and message conversion."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)

from harborrag_adapters.models.chat.langchain import to_harbor_message, to_langchain_message
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborToolCall,
    HarborToolCallFunction,
    MessageRole,
)

from .chat_client_support import FakeInvocation, response_dict
from .langchain_model_support import Summary, Weather, make_model, tool_call_response

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_tool_call_round_trip(config) -> None:
    invocation = FakeInvocation([tool_call_response(), response_dict("It is sunny in Paris")])
    model = make_model(config, invocation).bind_tools([Weather], tool_choice="auto")

    first = model.invoke([HumanMessage(content="weather in Paris?")])

    assert isinstance(first, AIMessage)
    assert first.tool_calls == [
        {"name": "Weather", "args": {"city": "Paris"}, "id": "call-weather", "type": "tool_call"}
    ]
    sent_tools = invocation.async_calls[0]["tools"]
    assert sent_tools[0]["function"]["name"] == "Weather"
    assert sent_tools[0]["function"]["parameters"]["properties"]["city"]["type"] == "string"
    assert invocation.async_calls[0]["tool_choice"] == "auto"

    second = model.invoke(
        [
            HumanMessage(content="weather in Paris?"),
            first,
            ToolMessage(content="sunny", tool_call_id="call-weather", name="Weather"),
        ]
    )

    assert second.content == "It is sunny in Paris"
    sent = invocation.async_calls[1]["messages"]
    assert sent[1]["role"] == "assistant"
    assert sent[1]["tool_calls"][0]["function"] == {
        "name": "Weather",
        "arguments": '{"city": "Paris"}',
    }
    assert sent[2] == {
        "role": "tool",
        "content": "sunny",
        "name": "Weather",
        "tool_call_id": "call-weather",
    }


def test_bind_tools_with_named_tool_choice_forces_that_tool(config) -> None:
    invocation = FakeInvocation([tool_call_response()])
    model = make_model(config, invocation).bind_tools([Weather], tool_choice="Weather")

    model.invoke("weather?")

    assert invocation.async_calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "Weather"},
    }


def test_with_structured_output_json_schema_returns_pydantic_instance(config) -> None:
    invocation = FakeInvocation([response_dict('{"title": "Memo", "score": 4}')])
    structured = make_model(config, invocation).with_structured_output(Summary)

    result = structured.invoke("summarize")

    assert result == Summary(title="Memo", score=4)
    response_format = invocation.async_calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "Summary"
    assert response_format["json_schema"]["schema"]["required"] == ["title", "score"]


def test_with_structured_output_function_calling_uses_forced_tool(config) -> None:
    raw = response_dict(None, finish_reason="tool_calls")
    raw["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call-summary",
            "type": "function",
            "index": 0,
            "function": {"name": "Summary", "arguments": '{"title":"Memo","score":2}'},
        }
    ]
    invocation = FakeInvocation([raw])
    structured = make_model(config, invocation).with_structured_output(
        Summary, method="function_calling", include_raw=True
    )

    result = structured.invoke("summarize")

    assert result["parsed"] == Summary(title="Memo", score=2)
    assert result["parsing_error"] is None
    assert isinstance(result["raw"], AIMessage)
    assert invocation.async_calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "Summary"},
    }


def test_sensitive_flag_and_metadata_propagate_to_request(config) -> None:
    invocation = FakeInvocation([])
    default_model = make_model(config, invocation)
    relaxed_model = make_model(config, invocation, sensitive=False)

    default_request = default_model.build_request([HumanMessage(content="hi")])
    relaxed_request = relaxed_model.build_request([HumanMessage(content="hi")], temperature=0.1)

    assert default_request.sensitive is True
    assert default_request.metadata.tenant_id == "tenant-1"
    assert default_request.logical_model == "primary"
    assert relaxed_request.sensitive is False
    assert relaxed_request.temperature == 0.1


def test_unknown_request_parameters_are_rejected(config) -> None:
    model = make_model(config, FakeInvocation([]))

    with pytest.raises(ValueError, match="unsupported HarborChatRequest parameters: frobnicate"):
        model.build_request([HumanMessage(content="hi")], frobnicate=1)


def test_trim_messages_uses_model_token_counter(config) -> None:
    model = make_model(config, FakeInvocation([]))
    history = [
        SystemMessage(content="system"),
        HumanMessage(content="a" * 70),
        AIMessage(content="b" * 70),
        HumanMessage(content="c" * 70),
        AIMessage(content="d" * 70),
        HumanMessage(content="latest question"),
    ]

    trimmed = trim_messages(
        history,
        max_tokens=60,
        token_counter=model,
        strategy="last",
        start_on="human",
        include_system=True,
    )

    assert trimmed[0] == history[0]
    assert trimmed[-1] == history[-1]
    assert len(trimmed) < len(history)
    assert model.get_num_tokens_from_messages(trimmed) <= 60
    assert model.get_num_tokens_from_messages(history) > 60


def test_message_conversion_round_trips_tool_calls() -> None:
    harbor = HarborChatMessage.assistant(
        "calling",
        tool_calls=[
            HarborToolCall(
                id="call-1",
                index=0,
                function=HarborToolCallFunction(name="Weather", arguments='{"city":"Rome"}'),
            ),
            HarborToolCall(
                id="call-2",
                index=1,
                function=HarborToolCallFunction(name="Weather", arguments='{"city":'),
            ),
        ],
    )

    converted = to_langchain_message(harbor)

    assert isinstance(converted, AIMessage)
    assert converted.tool_calls[0]["args"] == {"city": "Rome"}
    assert converted.invalid_tool_calls[0]["id"] == "call-2"
    back = to_harbor_message(converted)
    assert back.role is MessageRole.ASSISTANT
    assert back.tool_calls[0].function.parsed_arguments == {"city": "Rome"}
    assert json.loads(back.tool_calls[0].function.arguments) == {"city": "Rome"}
    assert back.tool_calls[1].function.arguments == '{"city":'

    tool_message = to_langchain_message(
        HarborChatMessage.tool("sunny", tool_call_id="call-1", name="Weather")
    )
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-1"
    assert to_harbor_message(tool_message).role is MessageRole.TOOL
