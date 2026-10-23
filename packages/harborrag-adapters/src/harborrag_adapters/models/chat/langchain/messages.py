"""Convert between LangChain messages and HarborRAG's provider-neutral chat messages."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.tool import InvalidToolCall, ToolCall

from harborrag_core.models.chat import (
    ContentPart,
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

# The engine estimates prompt size as ceil(characters / 3.5); the shim uses the
# same heuristic so ``trim_messages`` budgets agree with HarborRAG's own budgets.
_CHARACTERS_PER_TOKEN = 3.5
_TOKENS_PER_MESSAGE = 4

_ROLE_BY_NAME: dict[str, MessageRole] = {role.value: role for role in MessageRole}


def content_text(content: Any) -> str:
    """Return the concatenated text of a LangChain message content value."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "text-plain"}:
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def to_harbor_messages(messages: Iterable[BaseMessage]) -> tuple[HarborChatMessage, ...]:
    """Convert LangChain messages into HarborRAG chat messages."""

    return tuple(to_harbor_message(message) for message in messages)


def to_harbor_message(message: BaseMessage) -> HarborChatMessage:
    """Convert one LangChain message into a HarborRAG chat message."""

    if isinstance(message, SystemMessage):
        return HarborChatMessage.system(content_text(message.content))
    if isinstance(message, HumanMessage):
        return HarborChatMessage.user(_user_content(message.content))
    if isinstance(message, AIMessage):
        text = content_text(message.content)
        return HarborChatMessage.assistant(
            text or None,
            tool_calls=_harbor_tool_calls(message.tool_calls, message.invalid_tool_calls),
        )
    if isinstance(message, ToolMessage):
        return HarborChatMessage.tool(
            content_text(message.content),
            tool_call_id=message.tool_call_id,
            name=message.name,
        )
    if isinstance(message, ChatMessage):
        role = _ROLE_BY_NAME.get(message.role)
        if role is None:
            raise ValueError(f"unsupported chat message role: {message.role!r}")
        return HarborChatMessage(role=role, content=content_text(message.content))
    raise TypeError(f"unsupported LangChain message type: {type(message).__name__}")


def to_langchain_messages(messages: Iterable[HarborChatMessage]) -> list[BaseMessage]:
    """Convert HarborRAG chat messages into LangChain messages."""

    return [to_langchain_message(message) for message in messages]


def to_langchain_message(message: HarborChatMessage) -> BaseMessage:
    """Convert one HarborRAG chat message into a LangChain message."""

    if message.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER}:
        return SystemMessage(content=_text_of(message))
    if message.role is MessageRole.USER:
        return HumanMessage(content=_langchain_user_content(message.content), name=message.name)
    if message.role is MessageRole.TOOL:
        return ToolMessage(
            content=_text_of(message),
            tool_call_id=message.tool_call_id or "",
            name=message.name,
        )
    tool_calls, invalid = langchain_tool_calls(message.tool_calls)
    return AIMessage(content=_text_of(message), tool_calls=tool_calls, invalid_tool_calls=invalid)


def langchain_tool_calls(
    calls: Sequence[HarborToolCall],
) -> tuple[list[ToolCall], list[InvalidToolCall]]:
    """Split HarborRAG tool calls into parsed LangChain tool calls and invalid ones."""

    parsed: list[ToolCall] = []
    invalid: list[InvalidToolCall] = []
    for call in calls:
        arguments = _parsed_arguments(call.function)
        if arguments is None:
            invalid.append(
                InvalidToolCall(
                    name=call.function.name,
                    args=call.function.arguments,
                    id=call.id,
                    error="tool call arguments are not a JSON object",
                    type="invalid_tool_call",
                )
            )
        else:
            parsed.append(
                ToolCall(name=call.function.name, args=arguments, id=call.id, type="tool_call")
            )
    return parsed, invalid


def response_to_ai_message(response: HarborChatResponse) -> AIMessage:
    """Build the LangChain assistant message for one normalized chat response."""

    tool_calls, invalid = langchain_tool_calls(response.tool_calls)
    return AIMessage(
        content=response.text,
        id=response.id,
        tool_calls=tool_calls,
        invalid_tool_calls=invalid,
        usage_metadata=usage_metadata(response.usage),
        response_metadata=response_metadata(response),
    )


def response_metadata(response: HarborChatResponse) -> dict[str, Any]:
    """Return the provider-neutral response facts LangChain callers can inspect."""

    finish_reason = response.finish_reason
    metadata: dict[str, Any] = {
        "logical_model": response.logical_model,
        "provider": response.provider,
        "provider_model": response.provider_model,
        "deployment": response.deployment,
        "finish_reason": getattr(finish_reason, "value", finish_reason),
        "request_id": response.request_id,
        "provider_request_id": response.provider_request_id,
        "response_id": response.id,
        "cache_hit": response.cache_hit,
    }
    if response.reasoning_content is not None:
        metadata["reasoning_content"] = response.reasoning_content
    return metadata


def usage_metadata(usage: HarborChatUsage | None) -> UsageMetadata | None:
    """Map HarborRAG token usage onto LangChain's ``UsageMetadata``."""

    if usage is None:
        return None
    total = usage.total_tokens or usage.prompt_tokens + usage.completion_tokens
    metadata = UsageMetadata(
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=total,
    )
    if usage.cache_read_input_tokens is not None or usage.cache_creation_input_tokens is not None:
        metadata["input_token_details"] = {
            "cache_read": usage.cache_read_input_tokens or 0,
            "cache_creation": usage.cache_creation_input_tokens or 0,
        }
    if usage.reasoning_tokens is not None:
        metadata["output_token_details"] = {"reasoning": usage.reasoning_tokens}
    return metadata


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` with the engine's character heuristic."""

    return math.ceil(len(text) / _CHARACTERS_PER_TOKEN)


def estimate_message_tokens(messages: Iterable[BaseMessage]) -> int:
    """Estimate the prompt tokens consumed by ``messages`` including per-message overhead."""

    total = 0
    for message in messages:
        text = content_text(message.content)
        if isinstance(message, AIMessage):
            text += "".join(
                f"{call['name']}{json.dumps(call['args'])}" for call in message.tool_calls
            )
        total += _TOKENS_PER_MESSAGE + estimate_tokens(text)
    return total


def _text_of(message: HarborChatMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    if message.content is None:
        return ""
    return "".join(part.text for part in message.content if isinstance(part, TextContentPart))


def _user_content(content: Any) -> str | list[ContentPart]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TypeError("human message content must be text or a list of content blocks")
    parts: list[ContentPart] = []
    for block in content:
        parts.append(_content_part(block))
    return parts


def _content_part(block: Any) -> ContentPart:
    if isinstance(block, str):
        return TextContentPart(text=block)
    if not isinstance(block, dict):
        raise TypeError("content blocks must be strings or mappings")
    kind = block.get("type")
    if kind in {"text", "text-plain"}:
        return TextContentPart(text=str(block.get("text", "")))
    if kind == "image_url":
        image = block.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        detail = image.get("detail") if isinstance(image, dict) else None
        return ImageURLContentPart(image_url=ImageURL(url=str(url), detail=detail))
    if kind == "image" and block.get("url"):
        return ImageURLContentPart(image_url=ImageURL(url=str(block["url"])))
    raise ValueError(f"unsupported content block type: {kind!r}")


def _langchain_user_content(content: str | tuple[ContentPart, ...] | None) -> str | list[Any]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return [part.model_dump(mode="json", exclude_none=True) for part in content]


def _harbor_tool_calls(
    calls: Sequence[ToolCall], invalid: Sequence[InvalidToolCall]
) -> list[HarborToolCall]:
    converted = [
        HarborToolCall(
            id=call["id"] or f"call-{index}",
            index=index,
            function=HarborToolCallFunction(
                name=call["name"],
                arguments=json.dumps(call["args"]),
                parsed_arguments=dict(call["args"]),
            ),
        )
        for index, call in enumerate(calls)
    ]
    offset = len(converted)
    for position, call in enumerate(invalid):
        converted.append(
            HarborToolCall(
                id=call["id"] or f"call-{offset + position}",
                index=offset + position,
                function=HarborToolCallFunction(
                    name=call["name"] or "unknown", arguments=call["args"] or ""
                ),
            )
        )
    return converted


def _parsed_arguments(function: HarborToolCallFunction) -> dict[str, Any] | None:
    if function.parsed_arguments is not None:
        return dict(function.parsed_arguments)
    try:
        value = json.loads(function.arguments) if function.arguments else {}
    except ValueError:
        return None
    return dict(value) if isinstance(value, dict) else None


__all__ = [
    "content_text",
    "estimate_message_tokens",
    "estimate_tokens",
    "langchain_tool_calls",
    "response_metadata",
    "response_to_ai_message",
    "to_harbor_message",
    "to_harbor_messages",
    "to_langchain_message",
    "to_langchain_messages",
    "usage_metadata",
]
