from __future__ import annotations

import pytest
from harborrag_adapters.models.chat import HarborChatClient
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatTool,
    HarborToolCall,
    HarborToolCallFunction,
    HarborToolFunction,
)
from harborrag_core.models.errors import HarborChatInvalidRequestError

from .chat_client_support import FakeInvocation, response_dict


def weather_tool() -> HarborChatTool:
    return HarborChatTool(
        function=HarborToolFunction(
            name="weather",
            description="Get weather",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            strict=True,
        )
    )


def test_tool_definitions_calls_and_parallel_arguments_are_normalized(base_config) -> None:
    raw = response_dict(None, finish_reason="tool_calls")
    raw["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call-weather",
            "type": "function",
            "index": 0,
            "function": {"name": "weather", "arguments": '{"city":"Paris"}'},
        },
        {
            "id": "call-broken",
            "type": "function",
            "index": 1,
            "function": {"name": "weather", "arguments": '{"city":'},
        },
    ]
    invocation = FakeInvocation([raw])

    response = HarborChatClient(base_config, invocation=invocation).chat(
        [HarborChatMessage.user("Compare weather")],
        tools=(weather_tool(),),
        tool_choice="auto",
        parallel_tool_calls=True,
    )

    call = invocation.calls[0]
    assert call["tools"] == [weather_tool().model_dump(mode="json", exclude_none=True)]
    assert call["tool_choice"] == "auto"
    assert call["parallel_tool_calls"] is True
    assert tuple(tool.index for tool in response.tool_calls) == (0, 1)
    assert response.tool_calls[0].function.parsed_arguments == {"city": "Paris"}
    assert response.tool_calls[1].function.arguments == '{"city":'
    assert response.tool_calls[1].function.parsed_arguments is None


def test_tool_result_messages_preserve_provider_fields_only(base_config) -> None:
    previous_call = HarborToolCall(
        id="call-1",
        index=0,
        function=HarborToolCallFunction(
            name="weather",
            arguments='{"city":"Paris"}',
            parsed_arguments={"city": "Paris"},
        ),
    )
    invocation = FakeInvocation([response_dict("It is sunny")])

    HarborChatClient(base_config, invocation=invocation).chat(
        [
            HarborChatMessage.user("Weather?"),
            HarborChatMessage.assistant(tool_calls=(previous_call,)),
            HarborChatMessage.tool("sunny", tool_call_id="call-1"),
        ]
    )

    messages = invocation.calls[0]["messages"]
    assert messages[1]["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "weather", "arguments": '{"city":"Paris"}'},
        }
    ]
    assert "parsed_arguments" not in str(messages[1])
    assert messages[2] == {
        "role": "tool",
        "content": "sunny",
        "tool_call_id": "call-1",
    }


def test_tool_choice_requires_definitions(base_config) -> None:
    client = HarborChatClient(base_config, invocation=FakeInvocation())

    with pytest.raises(HarborChatInvalidRequestError, match="tool_choice"):
        client.chat([HarborChatMessage.user("hello")], tool_choice="auto")
