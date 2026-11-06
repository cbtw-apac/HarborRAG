"""Convert between persisted ``ConversationMessage`` rows and LangChain messages."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from harborrag_core.ports.conversation import (
    ConversationMessage,
    ConversationRole,
    new_message_id,
)

logger = logging.getLogger("harborrag.memory.langchain.converters")

type Clock = Callable[[], datetime]

_CREATED_AT_KEY = "harborrag_created_at"
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
    return AIMessage(content=message.content, id=message.message_id, additional_kwargs=extras)


def from_langchain_messages(
    messages: Iterable[BaseMessage],
    *,
    clock: Clock = utc_now,
) -> tuple[ConversationMessage, ...]:
    """Convert LangChain messages into conversation rows ready to append."""

    return tuple(from_langchain_message(message, clock=clock) for message in messages)


def from_langchain_message(
    message: BaseMessage,
    *,
    clock: Clock = utc_now,
) -> ConversationMessage:
    """Convert one LangChain message into a persisted conversation row.

    A missing ``id`` receives a fresh ``new_message_id`` so the caller can
    reference the row it is about to write. ``created_at`` round-trips through
    ``additional_kwargs`` when the message came from ``to_langchain_message``;
    otherwise ``clock()`` applies.

    Raises:
        TypeError: If the message is not a system, human or AI message.
    """

    role = _role_of(message)
    extras = message.additional_kwargs
    citations = extras.get(_CITATIONS_KEY)
    return ConversationMessage(
        message_id=message.id or new_message_id(),
        role=role,
        content=message.text,
        created_at=_created_at(extras.get(_CREATED_AT_KEY), clock),
        token_count=_optional_int(extras.get(_TOKEN_COUNT_KEY)),
        citations_json=json.dumps(citations) if citations is not None else None,
    )


def _role_of(message: BaseMessage) -> ConversationRole:
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    raise TypeError(f"unsupported LangChain message type: {type(message).__name__}")


def _additional_kwargs(message: ConversationMessage) -> dict[str, Any]:
    extras: dict[str, Any] = {_CREATED_AT_KEY: message.created_at.isoformat()}
    if message.token_count is not None:
        extras[_TOKEN_COUNT_KEY] = message.token_count
    if message.citations_json:
        citations = _decode(message.citations_json, field="citations_json")
        if citations is not None:
            extras[_CITATIONS_KEY] = citations
    return extras


def _decode(payload: str, *, field: str) -> Any:
    """Parse a persisted JSON column, or ``None`` when the row is corrupt.

    One unparseable row used to raise out of ``aget_messages`` and make the
    entire conversation unreadable. A message with unreadable metadata is worth
    more than no conversation, so the metadata is dropped and the text kept.
    """

    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        logger.warning("discarding unparseable %s on a stored message", field)
        return None


def _created_at(value: Any, clock: Clock) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return clock()


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "Clock",
    "from_langchain_message",
    "from_langchain_messages",
    "to_langchain_message",
    "to_langchain_messages",
    "utc_now",
]
