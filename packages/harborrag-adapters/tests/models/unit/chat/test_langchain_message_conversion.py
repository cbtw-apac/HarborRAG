"""Edge cases of the LangChain <-> HarborRAG chat message conversion helpers."""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    ChatMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from harborrag_adapters.models.chat.langchain.messages import (
    content_text,
    estimate_message_tokens,
    estimate_tokens,
    langchain_tool_calls,
    response_metadata,
    response_to_ai_message,
    to_harbor_message,
    to_harbor_messages,
    to_langchain_message,
    to_langchain_messages,
    usage_metadata,
)
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
    ImageURL,
    ImageURLContentPart,
    MessageRole,
    TextContentPart,
)

pytestmark = [pytest.mark.unit]

_IMAGE = "https://example.test/cat.png"


def _call(call_id: str, name: str, arguments: str) -> HarborToolCall:
    return HarborToolCall(
        id=call_id, function=HarborToolCallFunction(name=name, arguments=arguments)
    )


def _response(**overrides: object) -> HarborChatResponse:
    fields: dict[str, object] = {
        "id": "resp-1",
        "logical_model": "chat",
        "provider": "openai",
        "provider_model": "gpt-test",
        "deployment": "primary",
        "message": HarborChatMessage.assistant("hello"),
        "finish_reason": "stop",
        "request_id": "req-1",
    }
    fields.update(overrides)
    return HarborChatResponse.model_validate(fields)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain", "plain"),
        (None, ""),
        (42, ""),
        (
            [
                "a",
                {"type": "text", "text": "b"},
                {"type": "text-plain", "text": "c"},
                {"type": "image_url", "image_url": {"url": _IMAGE}},
                {"type": "text"},
                7,
            ],
            "abc",
        ),
    ],
)
def test_content_text_keeps_only_textual_blocks(content: object, expected: str) -> None:
    assert content_text(content) == expected


def test_human_message_blocks_become_typed_content_parts() -> None:
    message = HumanMessage(
        content=[
            "look at",
            {"type": "text", "text": "these"},
            {"type": "image_url", "image_url": {"url": _IMAGE, "detail": "low"}},
            {"type": "image_url", "image_url": _IMAGE},
            {"type": "image", "url": _IMAGE},
        ]
    )

    converted = to_harbor_message(message)

    assert converted.role is MessageRole.USER
    assert converted.content == (
        TextContentPart(text="look at"),
        TextContentPart(text="these"),
        ImageURLContentPart(image_url=ImageURL(url=_IMAGE, detail="low")),
        ImageURLContentPart(image_url=ImageURL(url=_IMAGE)),
        ImageURLContentPart(image_url=ImageURL(url=_IMAGE)),
    )


@pytest.mark.parametrize(
    ("block", "error", "match"),
    [
        (3, TypeError, "content blocks must be strings or mappings"),
        ({"type": "audio"}, ValueError, "unsupported content block type: 'audio'"),
        ({"type": "image"}, ValueError, "unsupported content block type: 'image'"),
    ],
)
def test_unsupported_human_content_blocks_are_rejected(
    block: object, error: type[Exception], match: str
) -> None:
    message = HumanMessage(content=["text"])
    message.content = ["text", block]  # type: ignore[list-item]

    with pytest.raises(error, match=match):
        to_harbor_message(message)


def test_human_content_that_is_neither_text_nor_blocks_is_rejected() -> None:
    message = HumanMessage(content="x")
    message.content = {"type": "text"}  # type: ignore[assignment]

    with pytest.raises(TypeError, match="human message content must be text"):
        to_harbor_message(message)


def test_generic_chat_message_maps_known_roles_and_rejects_unknown_ones() -> None:
    assert to_harbor_message(ChatMessage(role="developer", content="rules")) == (
        HarborChatMessage(role=MessageRole.DEVELOPER, content="rules")
    )

    with pytest.raises(ValueError, match="unsupported chat message role: 'wizard'"):
        to_harbor_message(ChatMessage(role="wizard", content="spell"))


def test_unknown_langchain_message_types_are_rejected() -> None:
    with pytest.raises(TypeError, match="unsupported LangChain message type: FunctionMessage"):
        to_harbor_message(FunctionMessage(name="f", content="x"))


def test_ai_message_tool_calls_keep_order_and_fill_missing_ids() -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"name": "search", "args": {"q": "x"}, "id": None, "type": "tool_call"}],
        invalid_tool_calls=[
            {"name": None, "args": None, "id": None, "error": "bad", "type": "invalid_tool_call"},
            {"name": "lookup", "args": "{oops", "id": "call-9", "error": "bad"},
        ],
    )

    converted = to_harbor_message(message)

    assert converted.content is None
    assert [(call.id, call.index) for call in converted.tool_calls] == [
        ("call-0", 0),
        ("call-1", 1),
        ("call-9", 2),
    ]
    assert converted.tool_calls[0].function.parsed_arguments == {"q": "x"}
    assert converted.tool_calls[1].function.name == "unknown"
    assert converted.tool_calls[1].function.arguments == ""
    assert converted.tool_calls[2].function.arguments == "{oops"


def test_to_harbor_messages_converts_every_message_in_order() -> None:
    converted = to_harbor_messages(
        [SystemMessage(content="sys"), ToolMessage(content="out", tool_call_id="c1", name="t")]
    )

    assert [message.role for message in converted] == [MessageRole.SYSTEM, MessageRole.TOOL]
    assert converted[1].tool_call_id == "c1"
    assert converted[1].name == "t"


def test_harbor_messages_convert_to_matching_langchain_types() -> None:
    converted = to_langchain_messages(
        [
            HarborChatMessage.developer("be brief"),
            HarborChatMessage.user(
                (TextContentPart(text="hi"), ImageURLContentPart(image_url=ImageURL(url=_IMAGE)))
            ),
            HarborChatMessage(role=MessageRole.ASSISTANT, content=None),
        ]
    )

    assert isinstance(converted[0], SystemMessage)
    assert converted[0].content == "be brief"
    assert isinstance(converted[1], HumanMessage)
    assert converted[1].content == [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": _IMAGE}},
    ]
    assert isinstance(converted[2], AIMessage)
    assert converted[2].content == ""


def test_assistant_list_content_keeps_only_text_parts() -> None:
    message = HarborChatMessage.model_construct(
        role=MessageRole.ASSISTANT,
        content=(
            TextContentPart(text="a"),
            ImageURLContentPart(image_url=ImageURL(url=_IMAGE)),
            TextContentPart(text="b"),
        ),
        tool_calls=(),
        name=None,
        tool_call_id=None,
    )

    assert to_langchain_message(message).content == "ab"


def test_user_message_without_content_becomes_empty_human_text() -> None:
    message = HarborChatMessage.model_construct(
        role=MessageRole.USER, content=None, tool_calls=(), name=None, tool_call_id=None
    )

    converted = to_langchain_message(message)

    assert isinstance(converted, HumanMessage)
    assert converted.content == ""


def test_tool_calls_split_into_parsed_and_invalid() -> None:
    parsed_already = HarborToolCall(
        id="c0",
        function=HarborToolCallFunction(
            name="pre", arguments='{"a": 1}', parsed_arguments={"a": 2}
        ),
    )
    calls = [
        parsed_already,
        _call("c1", "empty", ""),
        _call("c2", "broken", "{not json"),
        _call("c3", "listy", "[1, 2]"),
    ]

    parsed, invalid = langchain_tool_calls(calls)

    assert parsed == [
        {"name": "pre", "args": {"a": 2}, "id": "c0", "type": "tool_call"},
        {"name": "empty", "args": {}, "id": "c1", "type": "tool_call"},
    ]
    assert [(call["id"], call["args"]) for call in invalid] == [
        ("c2", "{not json"),
        ("c3", "[1, 2]"),
    ]
    assert {call["error"] for call in invalid} == {"tool call arguments are not a JSON object"}


def test_usage_metadata_maps_cache_and_reasoning_details() -> None:
    assert usage_metadata(None) is None

    plain = usage_metadata(HarborChatUsage(prompt_tokens=3, completion_tokens=2))
    assert plain == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}

    detailed = usage_metadata(
        HarborChatUsage(
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=20,
            cache_read_input_tokens=6,
            reasoning_tokens=1,
        )
    )
    assert detailed == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 20,
        "input_token_details": {"cache_read": 6, "cache_creation": 0},
        "output_token_details": {"reasoning": 1},
    }


def test_response_metadata_reports_reasoning_only_when_present() -> None:
    without = response_metadata(_response())
    assert without["finish_reason"] == "stop"
    assert without["response_id"] == "resp-1"
    assert without["request_id"] == "req-1"
    assert "reasoning_content" not in without

    assert response_metadata(_response(reasoning_content="why"))["reasoning_content"] == "why"


def test_response_to_ai_message_carries_tool_calls_usage_and_metadata() -> None:
    message = HarborChatMessage.assistant(None, tool_calls=[_call("c1", "search", '{"q": "x"}')])
    response = _response(
        message=message, usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1)
    )

    ai = response_to_ai_message(response)

    assert ai.id == "resp-1"
    assert ai.content == ""
    assert ai.tool_calls == [
        {"name": "search", "args": {"q": "x"}, "id": "c1", "type": "tool_call"}
    ]
    assert ai.usage_metadata == {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    assert ai.response_metadata["provider_model"] == "gpt-test"


def test_token_estimates_count_text_tool_calls_and_message_overhead() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefg") == 2

    ai = AIMessage(
        content="", tool_calls=[{"name": "ab", "args": {}, "id": "c", "type": "tool_call"}]
    )
    # "ab{}" is four characters -> ceil(4 / 3.5) == 2, plus four tokens of overhead.
    assert estimate_message_tokens([ai]) == 6
    assert estimate_message_tokens([HumanMessage(content="abcdefg"), ai]) == 6 + 6


def test_text_user_and_tool_messages_round_trip_through_langchain() -> None:
    user = to_langchain_message(HarborChatMessage.user("question"))
    tool = to_langchain_message(HarborChatMessage.tool("answer", tool_call_id="c1", name="t"))

    assert isinstance(user, HumanMessage)
    assert user.content == "question"
    assert isinstance(tool, ToolMessage)
    assert (tool.content, tool.tool_call_id, tool.name) == ("answer", "c1", "t")
    assert to_harbor_message(user) == HarborChatMessage.user("question")
