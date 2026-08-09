"""Tests for bounded runtime conversation memory."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.memory import (
    ConversationIdentity,
    ConversationTurn,
    DatabaseConversationMemory,
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


@pytest.mark.parametrize(
    ("max_sessions", "max_turns"),
    [(0, 1), (1, 0)],
)
def test_memory_rejects_non_positive_bounds(max_sessions: int, max_turns: int) -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        InMemoryConversationMemory(max_sessions=max_sessions, max_turns=max_turns)


@pytest.mark.asyncio
async def test_memory_validates_operations_on_unknown_sessions() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session")

    assert await memory.exists(identity) is False
    with pytest.raises(ValueError, match="session does not exist"):
        await memory.append(identity, ConversationTurn("question", "answer"))
    with pytest.raises(ValueError, match="limit must be positive"):
        await memory.recent(identity, limit=0)
    await memory.clear(identity)
    await memory.create(identity)
    await memory.create(identity)
    assert await memory.exists(identity) is True


@pytest.mark.asyncio
async def test_database_memory_delegates_and_closes_its_engine() -> None:
    repository = SimpleNamespace(
        recent=AsyncMock(return_value=(ConversationTurn("question", "answer"),)),
        create=AsyncMock(),
        exists=AsyncMock(return_value=True),
        append=AsyncMock(),
        clear=AsyncMock(),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    memory = DatabaseConversationMemory(
        repository=cast(Any, repository),
        engine=cast(Any, engine),
    )
    identity = ConversationIdentity("tenant", "principal", "session")
    turn = ConversationTurn("question", "answer")

    assert await memory.recent(identity, limit=5) == (turn,)
    await memory.create(identity)
    assert await memory.exists(identity) is True
    await memory.append(identity, turn)
    await memory.clear(identity)
    await memory.aclose()

    repository.recent.assert_awaited_once_with(identity, limit=5)
    repository.create.assert_awaited_once_with(identity)
    repository.exists.assert_awaited_once_with(identity)
    repository.append.assert_awaited_once_with(identity, turn)
    repository.clear.assert_awaited_once_with(identity)
    engine.dispose.assert_awaited_once_with()
