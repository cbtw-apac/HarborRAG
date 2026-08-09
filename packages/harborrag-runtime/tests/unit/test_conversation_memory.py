"""Tests for bounded runtime conversation memory."""

from __future__ import annotations

import pytest

from harborrag_runtime.memory import (
    ConversationIdentity,
    ConversationTurn,
    InMemoryConversationMemory,
)


@pytest.mark.asyncio
async def test_memory_isolates_identity_and_bounds_history() -> None:
    memory = InMemoryConversationMemory(max_sessions=2, max_turns=2)
    first = ConversationIdentity("tenant", "principal", "session-1")
    second = ConversationIdentity("tenant", "principal", "session-2")

    await memory.create(first)

    await memory.append(
        first,
        ConversationTurn("old", "old answer"),
    )
    await memory.append(first, ConversationTurn("middle", "middle answer"))
    await memory.append(first, ConversationTurn("new", "new answer"))

    assert [turn.user_content for turn in await memory.recent(first)] == ["middle", "new"]
    assert await memory.recent(second) == ()


@pytest.mark.asyncio
async def test_memory_evicts_least_recently_used_session_and_clears() -> None:
    memory = InMemoryConversationMemory(max_sessions=2)
    identities = [
        ConversationIdentity("tenant", "principal", f"session-{index}") for index in range(3)
    ]
    for identity in identities:
        await memory.create(identity)
        await memory.append(identity, ConversationTurn(identity.session_id, "answer"))

    assert await memory.recent(identities[0]) == ()
    assert await memory.recent(identities[2])
    await memory.clear(identities[2])
    assert await memory.recent(identities[2]) == ()
