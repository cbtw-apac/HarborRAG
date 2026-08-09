"""Top-level memory orchestration facade.

Chat and agent code can use this as a single entry point while the actual
storage and retrieval responsibilities remain split between the tier-specific
facades and their adapter-backed repositories.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from harborrag_core.chunking.metadata import FrozenMetadata
from harborrag_core.ports.conversation import ConversationTurn
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery

from .config import MemoryManagerConfig
from .errors import MemoryConfigurationError, MemoryScopeError
from .tiers import LongTermMemory, ShortTermMemory, WorkingMemory


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """One read-only view of the caller's current memory state."""

    recent_turns: tuple[ConversationTurn, ...] = ()
    working_state: Mapping[str, Any] = field(default_factory=FrozenMetadata)
    memories: tuple[Memory, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "working_state", FrozenMetadata(self.working_state))


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

    async def recent(
        self, owner: MemoryOwner, *, limit: int | None = None
    ) -> tuple[ConversationTurn, ...]:
        selected_limit = self._config.recent_turn_limit if limit is None else limit
        return await self._require_short_term().recent(owner, limit=selected_limit)

    async def append(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        await self._require_short_term().append(owner, turn)

    async def clear(self, owner: MemoryOwner) -> None:
        await self._require_short_term().clear(owner)

    async def scratch(self, owner: MemoryOwner) -> dict[str, Any]:
        return await self._require_working().scratch(owner)

    async def update(
        self,
        owner: MemoryOwner,
        state: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        await self._require_working().update(owner, state, ttl_seconds=ttl_seconds)

    async def clear_working(self, owner: MemoryOwner) -> None:
        await self._require_working().clear(owner)

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
        working_call = (
            self._working.scratch(owner) if self._working is not None else _empty_working_state()
        )
        memory_call = (
            self._long_term.search(owner, query)
            if query is not None and self._long_term is not None
            else _empty_memories()
        )
        recent_turns, working_state, memories = await asyncio.gather(
            recent_call, working_call, memory_call
        )
        return MemorySnapshot(
            recent_turns=recent_turns,
            working_state=working_state,
            memories=memories,
        )

    def _require_short_term(self) -> ShortTermMemory:
        if self._short_term is None:
            raise MemoryConfigurationError("short-term memory is not configured")
        return self._short_term

    def _require_working(self) -> WorkingMemory:
        if self._working is None:
            raise MemoryConfigurationError("working memory is not configured")
        return self._working

    def _require_long_term(self) -> LongTermMemory:
        if self._long_term is None:
            raise MemoryConfigurationError("long-term memory is not configured")
        return self._long_term


async def _empty_turns() -> tuple[ConversationTurn, ...]:
    return ()


async def _empty_working_state() -> dict[str, Any]:
    return {}


async def _empty_memories() -> tuple[Memory, ...]:
    return ()
