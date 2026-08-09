"""Short-term (conversation) memory tier."""

from __future__ import annotations

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationRepository,
    ConversationTurn,
)
from harborrag_core.ports.memory import MemoryOwner

from ..errors import MemoryScopeError


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

    async def record(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        """Compatibility alias for :meth:`append`."""

        await self.append(owner, turn)

    async def clear(self, owner: MemoryOwner) -> None:
        await self._conversation.clear(_conversation_identity(owner))


def _conversation_identity(owner: MemoryOwner) -> ConversationIdentity:
    if owner.principal_id is None or owner.session_id is None:
        raise MemoryScopeError("short-term memory requires principal_id and session_id")
    return ConversationIdentity(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=owner.session_id,
    )
