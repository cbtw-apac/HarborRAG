"""Defensive helpers for the two per-turn ``BaseChatModel`` calls."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from harborrag_core.ports.conversation import ConversationMessage

from .prompts import NO_MESSAGES_PLACEHOLDER

_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool result",
    "system": "System",
}


async def generate_text(model: BaseChatModel, *, system: str, user: str) -> str:
    """Ask ``model`` for one plain-text completion and return it stripped."""

    response = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    return message_text(response)


def message_text(message: BaseMessage) -> str:
    """Read a message's text whether ``text`` is a property or a method.

    ``langchain_core`` moved ``BaseMessage.text`` from a method to a property,
    and content blocks may arrive as a list instead of a string; both shapes
    are handled here so a provider change cannot fail a turn. The string form
    is preferred over the callable one so the deprecated call path is never
    taken on a version that still offers both.
    """

    raw: Any = getattr(message, "text", None)
    if not isinstance(raw, str) and callable(raw):
        raw = raw()
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return content_text(message.content)


def content_text(content: Any) -> str:
    """Flatten string or content-block message content into plain text."""

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [block.get("text", "") if isinstance(block, dict) else block for block in content]
        return "".join(str(part) for part in parts).strip()
    return ""


def render_transcript(messages: Iterable[ConversationMessage]) -> str:
    """Render conversation rows as labelled oldest-first transcript lines."""

    lines = [
        f"{_ROLE_LABELS.get(message.role, message.role)}: {message.content.strip()}"
        for message in messages
        if message.content.strip()
    ]
    return "\n".join(lines) if lines else NO_MESSAGES_PLACEHOLDER


__all__ = ["content_text", "generate_text", "message_text", "render_transcript"]
