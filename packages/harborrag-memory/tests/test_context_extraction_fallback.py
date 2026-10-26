"""Extraction survives a provider that refuses the structured-output schema.

``condense_question`` has always had a plain-text lane to fall back to;
extraction did not, so one provider rejecting the schema meant nothing was
ever remembered -- silently, because the caller swallows the failure and
returns no facts.
"""

from __future__ import annotations

import pytest
from context_test_fakes import NOW, MemoryRepositoryFake, row, structured_model

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_core.ports.memory import MemoryOwner, MemoryType
from harborrag_memory import MemoryExtractor, MemoryPolicy

pytestmark = [pytest.mark.unit]

MESSAGES: tuple[ConversationMessage, ...] = (
    row("user", "I always want answers in Vietnamese.", message_id="m-1"),
    row("assistant", "Understood, Vietnamese from now on.", message_id="m-2"),
)


@pytest.mark.asyncio
async def test_extract_falls_back_to_json_when_structured_output_is_rejected(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    """A provider that refuses the structured schema must not cost every memory.

    ``condense_question`` already degrades to a plain-text call when the
    structured lane fails; extraction gave up instead, so one provider that
    rejects the schema meant nothing was ever remembered -- and the failure
    was invisible, because the caller swallows it and returns no facts.
    """

    model = structured_model(
        structured_failure="Invalid schema for response_format: additionalProperties",
        replies=[
            '```json\n{"facts": [{"content": "The user prefers Vietnamese answers.",'
            ' "scope": "user", "memory_type": "preference", "importance": 0.8}]}\n```'
        ],
    )
    extractor = MemoryExtractor(
        policy=MemoryPolicy(),
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )

    saved = await extractor.extract(owner, messages=MESSAGES)

    assert [memory.content for memory in saved] == ["The user prefers Vietnamese answers."]
    assert saved[0].memory_type is MemoryType.PREFERENCE


@pytest.mark.asyncio
async def test_extract_returns_nothing_when_both_lanes_fail(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    """The fallback is best-effort: unparseable text is no facts, never a raise."""

    model = structured_model(
        structured_failure="schema rejected",
        replies=["I could not find anything worth remembering, sorry!"],
    )
    extractor = MemoryExtractor(
        policy=MemoryPolicy(),
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )

    assert await extractor.extract(owner, messages=MESSAGES) == ()
    assert memories.saved == []
