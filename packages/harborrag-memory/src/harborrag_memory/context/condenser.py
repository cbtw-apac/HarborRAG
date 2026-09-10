"""Condense the latest question into one self-contained retrieval query.

The same call also reports which *kinds* of memory the question is asking
for, so the type judgement costs nothing beyond the rewrite that was already
being paid for. A model advertising structured output returns the standalone
query plus at most ``MAX_WANTED_TYPES`` memory types; a model without it, or
a structured call that goes wrong in any way, falls back to the plain-text
rewrite and hints nothing at all.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_core.ports.memory import MemoryType

from .prompting import generate_text, render_transcript
from .prompts import (
    CONDENSE_SYSTEM_PROMPT,
    CONDENSE_TYPED_SYSTEM_PROMPT,
    CONDENSE_USER_TEMPLATE,
    NO_SUMMARY_PLACEHOLDER,
)
from .recall import RECALL_MEMORY_TYPES
from .strict_schema import STRICT_SCHEMA_CONFIG

logger = logging.getLogger(__name__)

_QUOTES = ('"', "'", "`", "“", "”", "‘", "’", "«", "»")

LENGTH_SLACK_CHARS = 200
"""Absolute head-room added to the 4x question-length rewrite sanity bound."""

LENGTH_MULTIPLIER = 4
"""A rewrite may resolve references, not turn into an essay."""

MAX_WANTED_TYPES = 3
"""How many memory types one question may ask for before the hint is noise."""

WANTED_TYPE_NAMES: dict[str, MemoryType] = {
    memory_type.value: memory_type for memory_type in RECALL_MEMORY_TYPES
}
"""The only type names a hint may use: exactly what recall can surface."""


@dataclass(frozen=True, slots=True)
class CondenseResult:
    """One usable rewrite plus the memory types the question asked for.

    ``wanted_types`` is empty whenever the model did not say, could not be
    asked, or proposed nothing recallable -- ranking then behaves exactly as
    it did before the hint existed.
    """

    query: str
    wanted_types: tuple[MemoryType, ...] = ()


class CondenseRequest(BaseModel):
    """The structured shape the model fills in: the rewrite and its type hint.

    Both fields are permissive and defaulted, like ``ProposedFacts``: a
    missing list means no hint, and an unknown or blank name is dropped
    rather than failing the whole response.
    """

    # Bound by ``with_structured_output``: see ``strict_schema`` for what
    # OpenAI rejects without this, and why it is declared on the schema
    # instead of enforced at parse time.
    model_config = STRICT_SCHEMA_CONFIG

    query: str = Field(default="", description="The self-contained rewritten question.")
    wanted_types: list[str] = Field(
        default_factory=list,
        description="At most three of: fact, preference, decision, episode. Empty when unsure.",
    )


async def condense_question(
    model: BaseChatModel,
    *,
    question: str,
    summary: str | None,
    messages: Sequence[ConversationMessage],
) -> CondenseResult | None:
    """Return a usable standalone query, or ``None`` when the model's is not."""

    user = CONDENSE_USER_TEMPLATE.format(
        summary=summary or NO_SUMMARY_PLACEHOLDER,
        transcript=render_transcript(messages),
        question=question,
    )
    if getattr(model, "supports_structured_output", True):
        typed = await _condense_typed(model, question=question, user=user)
        if typed is not None:
            return typed
    text = await generate_text(model, system=CONDENSE_SYSTEM_PROMPT, user=user)
    query = sanitize_query(text, question=question)
    return CondenseResult(query=query) if query is not None else None


async def _condense_typed(
    model: BaseChatModel,
    *,
    question: str,
    user: str,
) -> CondenseResult | None:
    """Rewrite and hint in one structured call, or ``None`` to take the plain path."""

    try:
        payload = await model.with_structured_output(CondenseRequest).ainvoke(
            [SystemMessage(content=CONDENSE_TYPED_SYSTEM_PROMPT), HumanMessage(content=user)]
        )
        request = as_request(payload)
    except Exception:
        logger.warning(
            "the structured condense call failed; rewriting as plain text", exc_info=True
        )
        return None
    query = sanitize_query(request.query, question=question)
    if query is None:
        return None
    return CondenseResult(query=query, wanted_types=recallable_types(request.wanted_types))


def as_request(payload: Any) -> CondenseRequest:
    """Read a structured-output payload however the provider shaped it."""

    if isinstance(payload, CondenseRequest):
        return payload
    if isinstance(payload, BaseModel):
        return CondenseRequest.model_validate(payload.model_dump())
    if isinstance(payload, dict):
        return CondenseRequest.model_validate(payload)
    raise TypeError(f"unsupported condense payload: {type(payload).__name__}")


def recallable_types(names: Sequence[str]) -> tuple[MemoryType, ...]:
    """Return the recallable types among ``names``, deduplicated and capped.

    Order is the model's own, so the cap keeps the types it thought most
    useful. A name recall cannot surface -- a summary, working state, or
    anything unknown -- is dropped rather than raising.
    """

    wanted = dict.fromkeys(
        memory_type
        for name in names
        if (memory_type := WANTED_TYPE_NAMES.get(name.strip().lower())) is not None
    )
    return tuple(wanted)[:MAX_WANTED_TYPES]


def sanitize_query(text: str, *, question: str) -> str | None:
    """Unwrap quotes and reject an empty or implausibly long rewrite.

    A model that ignores "output only the query" tends to answer the question
    instead; length is the cheap, provider-neutral signal for that, and the
    caller falls back to the original question when this returns ``None``.
    """

    cleaned = _unquote(text.strip())
    if not cleaned:
        return None
    if len(cleaned) > LENGTH_MULTIPLIER * len(question) + LENGTH_SLACK_CHARS:
        return None
    return cleaned


def _unquote(text: str) -> str:
    while len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text


__all__ = [
    "LENGTH_MULTIPLIER",
    "LENGTH_SLACK_CHARS",
    "MAX_WANTED_TYPES",
    "WANTED_TYPE_NAMES",
    "CondenseRequest",
    "CondenseResult",
    "as_request",
    "condense_question",
    "recallable_types",
    "sanitize_query",
]
