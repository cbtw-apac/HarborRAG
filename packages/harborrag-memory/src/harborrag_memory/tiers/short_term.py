"""Short-term (conversation) memory tier."""

from __future__ import annotations

from harborrag_core.ports.conversation import ConversationRepository, ConversationTurn
from harborrag_core.ports.memory import Memory, MemoryOwner


class ShortTermMemory:
    """Recent-turn memory for one authenticated conversation session."""

    def __init__(self, conversation: ConversationRepository) -> None:
        # TODO: wire the conversation repository dependency.
        pass

    async def recent(self, owner: MemoryOwner, *, limit: int = 2) -> tuple[Memory, ...]:
        # TODO: fetch recent turns from the conversation repository and map them to Memory.
        raise NotImplementedError("TODO: implement ShortTermMemory.recent")

    async def record(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        # TODO: append the turn to the conversation repository.
        raise NotImplementedError("TODO: implement ShortTermMemory.record")

    async def clear(self, owner: MemoryOwner) -> None:
        # TODO: clear the conversation repository for this owner.
        raise NotImplementedError("TODO: implement ShortTermMemory.clear")