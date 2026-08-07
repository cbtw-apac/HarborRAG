"""Unit coverage for the short-term memory tier's delegation to ConversationRepository."""

from __future__ import annotations

import pytest

from harborrag_core.ports.conversation import ConversationIdentity, ConversationTurn
from harborrag_core.ports.memory import MemoryOwner, MemoryScope, MemoryType
from harborrag_memory.tiers.short_term import ShortTermMemory


class _FakeConversationRepository:
    def __init__(self) -> None:
        self.turns: dict[ConversationIdentity, list[ConversationTurn]] = {}

    async def create(self, identity: ConversationIdentity) -> None:
        self.turns.setdefault(identity, [])

    async def exists(self, identity: ConversationIdentity) -> bool:
        return identity in self.turns

    async def recent(
        self, identity: ConversationIdentity, *, limit: int = 2
    ) -> tuple[ConversationTurn, ...]:
        return tuple(self.turns.get(identity, [])[-limit:])

    async def append(self, identity: ConversationIdentity, turn: ConversationTurn) -> None:
        self.turns.setdefault(identity, []).append(turn)

    async def clear(self, identity: ConversationIdentity) -> None:
        self.turns[identity] = []


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_record_and_recent_round_trip_through_conversation_repository() -> None:
    conversation = _FakeConversationRepository()
    identity = ConversationIdentity("ACME", "user-1", "session-1")
    await conversation.create(identity)
    short_term = ShortTermMemory(conversation)
    owner = MemoryOwner(tenant_id="ACME", principal_id="user-1", session_id="session-1")

    await short_term.record(owner, ConversationTurn("hi", "hello there"))
    memories = await short_term.recent(owner)

    assert len(memories) == 1
    assert memories[0].memory_type is MemoryType.CONVERSATION
    assert memories[0].scope is MemoryScope.SESSION
    assert "hi" in memories[0].content
    assert "hello there" in memories[0].content


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_recent_requires_a_session_scoped_owner() -> None:
    conversation = _FakeConversationRepository()
    short_term = ShortTermMemory(conversation)
    owner_without_session = MemoryOwner(tenant_id="ACME", principal_id="user-1")

    with pytest.raises(ValueError, match="session_id"):
        await short_term.recent(owner_without_session)
