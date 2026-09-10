"""Tests for bounded runtime conversation memory."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.memory import (
    ConversationHistoryRepository,
    ConversationIdentity,
    ConversationMessage,
    ConversationRole,
    ConversationTurn,
    DatabaseConversationMemory,
    InMemoryConversationMemory,
)


@pytest.mark.asyncio
async def test_memory_isolates_identity_and_bounds_history() -> None:
    memory = InMemoryConversationMemory(max_sessions=2, max_turns=2)
    first = ConversationIdentity("tenant", "principal", "session-1", "user")
    second = ConversationIdentity("tenant", "principal", "session-2", "user")

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
        ConversationIdentity("tenant", "principal", f"session-{index}", "user")
        for index in range(3)
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
    identity = ConversationIdentity("tenant", "principal", "session", "user")

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
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    turn = ConversationTurn("question", "answer")

    assert await memory.recent(identity, limit=5) == (turn,)
    await memory.create(identity)
    assert await memory.exists(identity) is True
    await memory.append(identity, turn)
    await memory.clear(identity)
    await memory.aclose()

    repository.recent.assert_awaited_once_with(identity, limit=5)
    repository.create.assert_awaited_once_with(identity, kind="chat", title=None)
    repository.exists.assert_awaited_once_with(identity, kind=None)
    repository.append.assert_awaited_once_with(identity, turn)
    repository.clear.assert_awaited_once_with(identity)
    engine.dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_memory_binds_sessions_to_their_kind() -> None:
    memory = InMemoryConversationMemory(max_sessions=1)
    chat = ConversationIdentity("tenant", "principal", "chat-session", "user")
    agent = ConversationIdentity("tenant", "principal", "agent-session", "user")

    await memory.create(chat)
    assert await memory.exists(chat, kind="chat") is True
    assert await memory.exists(chat, kind="agent") is False
    assert await memory.exists(chat) is True

    await memory.create(agent, kind="agent")
    assert await memory.exists(agent, kind="agent") is True
    assert await memory.exists(agent, kind="chat") is False
    # Eviction drops the kind record together with the session.
    assert await memory.exists(chat) is False
    assert memory._state.keys() == {agent}


@pytest.mark.asyncio
async def test_database_memory_forwards_session_kind() -> None:
    repository = SimpleNamespace(create=AsyncMock(), exists=AsyncMock(return_value=False))
    memory = DatabaseConversationMemory(
        repository=cast(Any, repository),
        engine=cast(Any, SimpleNamespace(dispose=AsyncMock())),
    )
    identity = ConversationIdentity("tenant", "principal", "session", "user")

    await memory.create(identity, kind="agent")
    assert await memory.exists(identity, kind="agent") is False

    repository.create.assert_awaited_once_with(identity, kind="agent", title=None)
    repository.exists.assert_awaited_once_with(identity, kind="agent")


def _message(message_id: str, role: ConversationRole, content: str) -> ConversationMessage:
    return ConversationMessage(message_id, role, content, datetime.now(UTC))


@pytest.mark.asyncio
async def test_in_memory_messages_round_trip_and_derive_turns() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    messages = (
        _message("m-1", "user", "q-1"),
        _message(
            "m-2",
            "assistant",
            "",
        ),
        _message("m-3", "tool", "result"),
        _message("m-4", "assistant", "a-1"),
    )
    await memory.append_messages(identity, messages)
    await memory.append(identity, ConversationTurn("q-2", "a-2"))

    stored = await memory.recent_messages(identity, limit=10)
    assert stored[:4] == messages
    assert [m.role for m in stored[4:]] == ["user", "assistant"]
    assert stored[4].message_id.startswith("msg-")
    assert await memory.recent_messages(identity, limit=1) == stored[-1:]
    assert await memory.recent(identity, limit=5) == (
        ConversationTurn("q-1", ""),
        ConversationTurn("q-2", "a-2"),
    )
    await memory.clear_messages(identity)
    assert await memory.recent_messages(identity, limit=10) == ()
    assert await memory.recent(identity) == ()


@pytest.mark.asyncio
async def test_in_memory_messages_after_pages_and_rejects_unknown_cursor() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    await memory.append_messages(
        identity, tuple(_message(f"m-{i}", "user", str(i)) for i in range(5))
    )

    first = await memory.messages_after(identity, after_message_id=None, limit=2)
    assert [m.message_id for m in first] == ["m-0", "m-1"]
    second = await memory.messages_after(identity, after_message_id="m-1", limit=2)
    assert [m.message_id for m in second] == ["m-2", "m-3"]
    assert await memory.messages_after(identity, after_message_id="m-4", limit=2) == ()
    with pytest.raises(ValueError, match="unknown conversation message cursor"):
        await memory.messages_after(identity, after_message_id="nope", limit=2)
    with pytest.raises(ValueError, match="limit must be positive"):
        await memory.recent_messages(identity, limit=0)


@pytest.mark.asyncio
async def test_in_memory_message_bound_is_two_messages_per_turn() -> None:
    memory = InMemoryConversationMemory(max_turns=1)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    await memory.append(identity, ConversationTurn("old", "old answer"))
    await memory.append(identity, ConversationTurn("new", "new answer"))

    assert [m.content for m in await memory.recent_messages(identity, limit=10)] == [
        "new",
        "new answer",
    ]
    with pytest.raises(ValueError, match="session does not exist"):
        await memory.append_messages(
            ConversationIdentity("tenant", "principal", "other", "user"),
            (_message("x", "user", "x"),),
        )
    with pytest.raises(ValueError, match="bounds must be positive"):
        InMemoryConversationMemory(max_messages=0)


def test_in_memory_memory_satisfies_the_history_repository_protocol() -> None:
    repository: ConversationHistoryRepository = InMemoryConversationMemory()
    assert isinstance(repository, InMemoryConversationMemory)


@pytest.mark.asyncio
async def test_database_memory_forwards_message_methods() -> None:
    message = _message("m-1", "user", "q")
    repository = SimpleNamespace(
        append_messages=AsyncMock(),
        recent_messages=AsyncMock(return_value=(message,)),
        messages_after=AsyncMock(return_value=(message,)),
        clear_messages=AsyncMock(),
    )
    memory = DatabaseConversationMemory(
        repository=cast(Any, repository),
        engine=cast(Any, SimpleNamespace(dispose=AsyncMock())),
    )
    identity = ConversationIdentity("tenant", "principal", "session", "user")

    await memory.append_messages(identity, (message,))
    assert await memory.recent_messages(identity, limit=3) == (message,)
    assert await memory.messages_after(identity, after_message_id="m-0", limit=3) == (message,)
    await memory.clear_messages(identity)

    repository.append_messages.assert_awaited_once_with(identity, (message,))
    repository.recent_messages.assert_awaited_once_with(identity, limit=3)
    repository.messages_after.assert_awaited_once_with(identity, after_message_id="m-0", limit=3)
    repository.clear_messages.assert_awaited_once_with(identity)
