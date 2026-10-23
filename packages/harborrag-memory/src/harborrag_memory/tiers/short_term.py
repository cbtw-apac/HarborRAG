"""Short-term (conversation) memory tier."""

from __future__ import annotations

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationRepository,
    ConversationTurn,
)
from harborrag_core.ports.memory import MemoryOwner

from ..identity import conversation_identity


class ShortTermMemory:
    """Recent-turn memory for one authenticated conversation session."""

    def __init__(self, conversation: ConversationRepository) -> None:
        self._conversation = conversation

    async def recent(self, owner: MemoryOwner, *, limit: int = 2) -> tuple[ConversationTurn, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("recent memory limit must be between 1 and 1000")
        return await self._conversation.recent(_conversation_identity(owner), limit=limit)

    async def append(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        await self._conversation.append(_conversation_identity(owner), turn)

    async def clear(self, owner: MemoryOwner) -> None:
        await self._conversation.clear(_conversation_identity(owner))


def _conversation_identity(owner: MemoryOwner) -> ConversationIdentity:
    return conversation_identity(owner, subject="short-term memory")
