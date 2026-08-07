"""Short-term (conversation) memory tier.

Delegates to the existing ``ConversationRepository`` port rather than
duplicating it -- short-term memory has one live implementation already
(SQL- and in-memory-backed, wired through every chat/agent call site), so
this tier is a thin ``Memory``-shaped view over it, not a new store.
"""

from __future__ import annotations

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationRepository,
    ConversationTurn,
)
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope, MemoryType, new_memory_id

_REQUIRED_FIELDS = ("tenant_id", "principal_id", "session_id")


def _require_session_owner(owner: MemoryOwner) -> tuple[str, str, str]:
    missing = [name for name in _REQUIRED_FIELDS if getattr(owner, name) is None]
    if missing:
        raise ValueError(f"short-term memory requires owner.{missing[0]}")
    return owner.tenant_id, owner.principal_id, owner.session_id  # type: ignore[return-value]


def _turn_to_memory(owner: MemoryOwner, turn: ConversationTurn) -> Memory:
    return Memory(
        memory_id=new_memory_id(),
        scope=MemoryScope.SESSION,
        memory_type=MemoryType.CONVERSATION,
        owner=owner,
        content=f"User: {turn.user_content}\nAssistant: {turn.assistant_content}",
    )


class ShortTermMemory:
    """Recent-turn memory for one authenticated conversation session."""

    def __init__(self, conversation: ConversationRepository) -> None:
        self._conversation = conversation

    async def recent(self, owner: MemoryOwner, *, limit: int = 2) -> tuple[Memory, ...]:
        tenant_id, principal_id, session_id = _require_session_owner(owner)
        turns = await self._conversation.recent(
            ConversationIdentity(tenant_id, principal_id, session_id), limit=limit
        )
        return tuple(_turn_to_memory(owner, turn) for turn in turns)

    async def record(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        tenant_id, principal_id, session_id = _require_session_owner(owner)
        await self._conversation.append(
            ConversationIdentity(tenant_id, principal_id, session_id), turn
        )

    async def clear(self, owner: MemoryOwner) -> None:
        tenant_id, principal_id, session_id = _require_session_owner(owner)
        await self._conversation.clear(ConversationIdentity(tenant_id, principal_id, session_id))


__all__ = ["ShortTermMemory"]
