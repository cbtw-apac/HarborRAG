"""Convert between persisted ``ConversationMessage`` rows and LangChain messages.

Tool calls travel as ``tool_calls_json``: a JSON list of LangChain ``ToolCall``
objects (``{"id", "name", "args", "type"}``). OpenAI-shaped entries
(``{"id", "function": {"name", "arguments"}}``) are accepted on read so rows
written by other producers still load.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall

from harborrag_core.ports.conversation import (
    ConversationMessage,
    ConversationRole,
    new_message_id,
)

type Clock = Callable[[], datetime]

_CREATED_AT_KEY = "harborrag_created_at"
_RUN_ID_KEY = "harborrag_run_id"
_TOKEN_COUNT_KEY = "harborrag_token_count"
_CITATIONS_KEY = "citations"


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""

    return datetime.now(UTC)


def to_langchain_messages(messages: Iterable[ConversationMessage]) -> list[BaseMessage]:
    """Convert oldest-first conversation rows into LangChain messages."""

    return [to_langchain_message(message) for message in messages]


def to_langchain_message(message: ConversationMessage) -> BaseMessage:
    """Convert one persisted conversation row into the matching LangChain message."""

    extras = _additional_kwargs(message)
    if message.role == "system":
        return SystemMessage(
            content=message.content, id=message.message_id, additional_kwargs=extras
        )
    if message.role == "user":
        return HumanMessage(
            content=message.content, id=message.message_id, additional_kwargs=extras
        )
    if message.role == "tool":
        return ToolMessage(
            content=message.content,
            tool_call_id=message.tool_call_id or "",
            id=message.message_id,
            additional_kwargs=extras,
        )
    return AIMessage(
        content=message.content,
        id=message.message_id,
        tool_calls=_tool_calls_from_json(message.tool_calls_json),
        additional_kwargs=extras,
    )


def from_langchain_messages(
    messages: Iterable[BaseMessage],
    *,
    run_id: str | None = None,
    clock: Clock = utc_now,
) -> tuple[ConversationMessage, ...]:
    """Convert LangChain messages into conversation rows ready to append."""

    return tuple(
        from_langchain_message(message, run_id=run_id, clock=clock) for message in messages
    )


def from_langchain_message(
    message: BaseMessage,
    *,
    run_id: str | None = None,
    clock: Clock = utc_now,
) -> ConversationMessage:
    """Convert one LangChain message into a persisted conversation row.

    A missing ``id`` receives a fresh ``new_message_id`` so the caller can
    reference the row it is about to write. ``created_at`` and ``run_id``
    round-trip through ``additional_kwargs`` when the message came from
    ``to_langchain_message``; otherwise ``clock()`` and ``run_id`` apply.
    """

    role = _role_of(message)
    extras = message.additional_kwargs
    tool_calls_json: str | None = None
    tool_call_id: str | None = None
    if isinstance(message, AIMessage) and message.tool_calls:
        tool_calls_json = json.dumps([dict(call) for call in message.tool_calls])
    if isinstance(message, ToolMessage):
        tool_call_id = message.tool_call_id
    citations = extras.get(_CITATIONS_KEY)
    return ConversationMessage(
        message_id=message.id or new_message_id(),
        role=role,
        content=message.text,
        created_at=_created_at(extras.get(_CREATED_AT_KEY), clock),
        token_count=_optional_int(extras.get(_TOKEN_COUNT_KEY)),
        tool_calls_json=tool_calls_json,
        tool_call_id=tool_call_id,
        citations_json=json.dumps(citations) if citations is not None else None,
        run_id=_optional_str(extras.get(_RUN_ID_KEY)) or run_id,
    )


def _role_of(message: BaseMessage) -> ConversationRole:
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, ToolMessage):
        return "tool"
    raise TypeError(f"unsupported LangChain message type: {type(message).__name__}")


def _additional_kwargs(message: ConversationMessage) -> dict[str, Any]:
    extras: dict[str, Any] = {_CREATED_AT_KEY: message.created_at.isoformat()}
    if message.run_id is not None:
        extras[_RUN_ID_KEY] = message.run_id
    if message.token_count is not None:
        extras[_TOKEN_COUNT_KEY] = message.token_count
    if message.citations_json:
        extras[_CITATIONS_KEY] = json.loads(message.citations_json)
    return extras


def _tool_calls_from_json(payload: str | None) -> list[ToolCall]:
    if not payload:
        return []
    entries = json.loads(payload)
    if not isinstance(entries, list):
        raise ValueError("tool_calls_json must encode a JSON list")
    return [_tool_call(entry) for entry in entries if isinstance(entry, dict)]


def _tool_call(entry: dict[str, Any]) -> ToolCall:
    function = entry.get("function")
    if isinstance(function, dict):
        arguments = function.get("arguments") or "{}"
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        return ToolCall(
            name=str(function.get("name", "")),
            args=args if isinstance(args, dict) else {},
            id=_optional_str(entry.get("id")),
            type="tool_call",
        )
    args = entry.get("args") or {}
    return ToolCall(
        name=str(entry.get("name", "")),
        args=dict(args) if isinstance(args, dict) else {},
        id=_optional_str(entry.get("id")),
        type="tool_call",
    )


def _created_at(value: Any, clock: Clock) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return clock()


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "Clock",
    "from_langchain_message",
    "from_langchain_messages",
    "to_langchain_message",
    "to_langchain_messages",
    "utc_now",
]
