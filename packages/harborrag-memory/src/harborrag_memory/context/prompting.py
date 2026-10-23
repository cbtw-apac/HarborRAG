"""Model-port dispatch and defensive text helpers for memory policies."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from harborrag_core.ports.conversation import ConversationMessage

from .model import MemoryModel, MemoryModelLike
from .prompts import NO_MESSAGES_PLACEHOLDER

TRANSCRIPT_MESSAGE_MAX_CHARS = 8_000
"""Longest message a memory prompt renders whole, roughly 2,000 tokens.

A pasted log or an unbounded assistant answer would otherwise push a summary,
condense, or extraction prompt past the memory model's context window on every
turn that covers it, so a longer message keeps its head and tail instead.
"""

ELISION_MARKER = "\n[... {omitted} characters omitted ...]\n"

_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool result",
    "system": "System",
}


def _model_port(model: MemoryModelLike) -> MemoryModel:
    if isinstance(model, MemoryModel):
        return model
    from ..langchain.model import LangChainMemoryModel

    return LangChainMemoryModel(model)


async def generate_text(model: MemoryModelLike, *, system: str, user: str) -> str:
    """Ask ``model`` for one plain-text completion and return it stripped."""

    return (await _model_port(model).generate_text(system=system, user=user)).strip()


async def generate_structured(
    model: MemoryModelLike, *, schema: type[BaseModel], system: str, user: str
) -> object:
    return await _model_port(model).generate_structured(schema=schema, system=system, user=user)


def message_text(message: Any) -> str:
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


def clip_message(text: str, max_chars: int = TRANSCRIPT_MESSAGE_MAX_CHARS) -> str:
    """Return ``text`` stripped, keeping its head and tail when it is too long."""

    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}{ELISION_MARKER.format(omitted=omitted)}{text[-tail:]}"


def render_transcript(messages: Iterable[ConversationMessage]) -> str:
    """Render conversation rows as labelled oldest-first transcript lines.

    Each message is clipped by ``clip_message``, so one huge message cannot
    make the prompt it lands in unanswerable.
    """

    lines = [
        f"{_ROLE_LABELS.get(message.role, message.role)}: {clip_message(message.content)}"
        for message in messages
        if message.content.strip()
    ]
    return "\n".join(lines) if lines else NO_MESSAGES_PLACEHOLDER


__all__ = [
    "ELISION_MARKER",
    "TRANSCRIPT_MESSAGE_MAX_CHARS",
    "clip_message",
    "content_text",
    "generate_text",
    "message_text",
    "render_transcript",
]
