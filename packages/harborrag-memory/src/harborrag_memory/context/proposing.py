"""Ask the model for the facts a conversation states, in two lanes.

Structured output first, plain JSON second -- the same shape
``condense_question`` uses. The second lane exists because a provider that
rejects the structured schema fails the *whole request* rather than
answering badly: without it, one such provider means nothing is ever
remembered, and silently, since the caller swallows the failure.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .facts import ProposedFacts, facts_from_text
from .prompting import generate_text
from .prompts import EXTRACTION_JSON_SYSTEM_PROMPT, EXTRACTION_SYSTEM_PROMPT

logger = logging.getLogger("harborrag.memory.context.proposing")


async def propose_facts(model: BaseChatModel, user: str) -> Any | None:
    """The model's proposed facts in whatever shape a lane produced them.

    ``None`` when neither lane returned anything usable; the caller then
    stores nothing, exactly as when the model proposed no facts at all.
    """

    if getattr(model, "supports_structured_output", True):
        payload = await _typed(model, user)
        if payload is not None:
            return payload
    return await _json(model, user)


async def _typed(model: BaseChatModel, user: str) -> Any | None:
    """The structured lane, or ``None`` to let the JSON lane try."""

    try:
        return await model.with_structured_output(ProposedFacts).ainvoke(
            [SystemMessage(content=EXTRACTION_SYSTEM_PROMPT), HumanMessage(content=user)]
        )
    except Exception:
        logger.warning(
            "the structured extraction call failed; asking for plain JSON", exc_info=True
        )
        return None


async def _json(model: BaseChatModel, user: str) -> ProposedFacts | None:
    """The plain-JSON lane; ``None`` when the reply carries no usable object."""

    return facts_from_text(
        await generate_text(model, system=EXTRACTION_JSON_SYSTEM_PROMPT, user=user)
    )


__all__ = ["propose_facts"]
