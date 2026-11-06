"""Top-level memory orchestration facade.

Chat code can use this as a single entry point while the actual
storage and retrieval responsibilities remain split between the tier-specific
facades and their adapter-backed repositories.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from harborrag_core.ports.conversation import ConversationTurn
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery

from .config import MemoryManagerConfig
from .errors import MemoryConfigurationError, MemoryScopeError
from .tiers import LongTermMemory, ShortTermMemory


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """One read-only view of the caller's current memory state."""

    recent_turns: tuple[ConversationTurn, ...] = ()
    memories: tuple[Memory, ...] = ()


class MemoryManager:
    """Single facade for short-term and long-term memory."""

    def __init__(
        self,
        short_term: ShortTermMemory | None = None,
        long_term: LongTermMemory | None = None,
        *,
        config: MemoryManagerConfig | None = None,
    ) -> None:
        self._short_term = short_term
        self._long_term = long_term
        self._config = config or MemoryManagerConfig()

    async def recent(
        self, owner: MemoryOwner, *, limit: int | None = None
    ) -> tuple[ConversationTurn, ...]:
        selected_limit = self._config.recent_turn_limit if limit is None else limit
        return await self._require_short_term().recent(owner, limit=selected_limit)

    async def append(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        await self._require_short_term().append(owner, turn)

    async def clear(self, owner: MemoryOwner) -> None:
        await self._require_short_term().clear(owner)

    async def save(self, caller: MemoryOwner, memory: Memory) -> None:
        await self._require_long_term().save(caller, memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        return await self._require_long_term().get(caller, memory_id)

    async def search(self, caller: MemoryOwner, query: MemoryQuery) -> tuple[Memory, ...]:
        return await self._require_long_term().search(caller, query)

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        await self._require_long_term().delete(caller, memory_id)

    async def snapshot(
        self,
        owner: MemoryOwner,
        *,
        query: MemoryQuery | None = None,
        recent_limit: int | None = None,
    ) -> MemorySnapshot:
        if query is not None and query.owner != owner:
            raise MemoryScopeError("snapshot query owner must match the authenticated caller")
        selected_limit = self._config.recent_turn_limit if recent_limit is None else recent_limit
        recent_call = (
            self._short_term.recent(owner, limit=selected_limit)
            if self._short_term is not None
            else _empty_turns()
        )
        if query is not None and self._long_term is None:
            # Returning () here made "nothing stored" and "no long-term tier
            # configured" indistinguishable, while search() raised for the
            # second. One question, one answer.
            raise MemoryConfigurationError("long-term memory is not configured")
        memory_call = (
            self._long_term.search(owner, query)
            if query is not None and self._long_term is not None
            else _empty_memories()
        )
        recent_turns, memories = await asyncio.gather(recent_call, memory_call)
        return MemorySnapshot(recent_turns=recent_turns, memories=memories)

    def _require_short_term(self) -> ShortTermMemory:
        if self._short_term is None:
            raise MemoryConfigurationError("short-term memory is not configured")
        return self._short_term

    def _require_long_term(self) -> LongTermMemory:
        if self._long_term is None:
            raise MemoryConfigurationError("long-term memory is not configured")
        return self._long_term


async def _empty_turns() -> tuple[ConversationTurn, ...]:
    return ()


async def _empty_memories() -> tuple[Memory, ...]:
    return ()
