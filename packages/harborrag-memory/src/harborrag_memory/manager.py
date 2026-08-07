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
from .errors import MemoryConfigurationError
from .long_term.memory import LongTermMemory
from .short_term.memory import ShortTermMemory
from .working.memory import WorkingMemory


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
        self._short_term = short_term
        self._working = working
        self._long_term = long_term
        self._config = config or MemoryManagerConfig()

    async def recent(self, owner: MemoryOwner, *, limit: int | None = None) -> tuple[ConversationTurn, ...]:
        if self._short_term is None:
            return ()
        return await self._short_term.recent(owner, limit=limit or self._config.recent_turn_limit)

    async def append(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        if self._short_term is None:
            raise MemoryConfigurationError("short-term memory is not configured")
        await self._short_term.record(owner, turn)

    async def clear(self, owner: MemoryOwner) -> None:
        if self._short_term is None:
            raise MemoryConfigurationError("short-term memory is not configured")
        await self._short_term.clear(owner)

    async def scratch(self, owner: MemoryOwner) -> dict[str, Any]:
        if self._working is None:
            return {}
        return await self._working.scratch(owner)

    async def update(
        self,
        owner: MemoryOwner,
        state: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        if self._working is None:
            raise MemoryConfigurationError("working memory is not configured")
        await self._working.update(
            owner,
            state,
            ttl_seconds=ttl_seconds or self._config.working_ttl_seconds,
        )

    async def clear_working(self, owner: MemoryOwner) -> None:
        if self._working is None:
            raise MemoryConfigurationError("working memory is not configured")
        await self._working.clear(owner)

    async def save(self, memory: Memory) -> None:
        if self._long_term is None:
            raise MemoryConfigurationError("long-term memory is not configured")
        await self._long_term.save(memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        if self._long_term is None:
            return None
        return await self._long_term.get(caller, memory_id)

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        if self._long_term is None:
            return ()
        return await self._long_term.search(query)

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        if self._long_term is None:
            raise MemoryConfigurationError("long-term memory is not configured")
        await self._long_term.delete(caller, memory_id)

    async def snapshot(
        self,
        owner: MemoryOwner,
        *,
        query: MemoryQuery | None = None,
        recent_limit: int | None = None,
    ) -> MemorySnapshot:
        memories = await self.search(query) if query is not None else ()
        return MemorySnapshot(
            recent_turns=await self.recent(owner, limit=recent_limit),
            working_state=await self.scratch(owner),
            memories=memories,
        )


__all__ = ["MemoryManager", "MemorySnapshot"]