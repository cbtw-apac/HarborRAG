"""Top-level memory orchestration facade.

Chat and agent code can use this as a single entry point while the actual
storage and retrieval responsibilities remain split between the tier-specific
facades and their adapter-backed repositories.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harborrag_core.ports.conversation import ConversationTurn
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery

from .config import MemoryManagerConfig
from .tiers import LongTermMemory, ShortTermMemory, WorkingMemory


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """One read-only view of the caller's current memory state."""

    recent_turns: tuple[ConversationTurn, ...] = ()
    working_state: dict[str, Any] = field(default_factory=dict)
    memories: tuple[Memory, ...] = ()


class MemoryManager:
    """Single facade for short-term, working, and long-term memory."""

    def __init__(
        self,
        short_term: ShortTermMemory | None = None,
        working: WorkingMemory | None = None,
        long_term: LongTermMemory | None = None,
        *,
        config: MemoryManagerConfig | None = None,
    ) -> None:
        # TODO: wire the tier facades and config dependency.
        pass

    async def recent(self, owner: MemoryOwner, *, limit: int | None = None) -> tuple[ConversationTurn, ...]:
        # TODO: delegate to the short-term memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.recent")

    async def append(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        # TODO: delegate to the short-term memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.append")

    async def clear(self, owner: MemoryOwner) -> None:
        # TODO: delegate to the short-term memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.clear")

    async def scratch(self, owner: MemoryOwner) -> dict[str, Any]:
        # TODO: delegate to the working memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.scratch")

    async def update(
        self,
        owner: MemoryOwner,
        state: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        # TODO: delegate to the working memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.update")

    async def clear_working(self, owner: MemoryOwner) -> None:
        # TODO: delegate to the working memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.clear_working")

    async def save(self, memory: Memory) -> None:
        # TODO: delegate to the long-term memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.save")

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        # TODO: delegate to the long-term memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.get")

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        # TODO: delegate to the long-term memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.search")

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        # TODO: delegate to the long-term memory tier.
        raise NotImplementedError("TODO: implement MemoryManager.delete")

    async def snapshot(
        self,
        owner: MemoryOwner,
        *,
        query: MemoryQuery | None = None,
        recent_limit: int | None = None,
    ) -> MemorySnapshot:
        # TODO: compose recent turns, working state, and searched memories into a snapshot.
        raise NotImplementedError("TODO: implement MemoryManager.snapshot")
