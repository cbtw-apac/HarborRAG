"""Long-term memory tier: durable, searchable memories scoped by owner."""

from __future__ import annotations

from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    visible_to,
)

from ..errors import MemoryScopeError


class LongTermMemory:
    """Facade over the canonical long-term memory repository."""

    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    async def save(self, caller: MemoryOwner, memory: Memory) -> None:
        _authorize_write(caller, memory)
        await self._repository.save(memory)

    async def remember(self, caller: MemoryOwner, memory: Memory) -> None:
        """Alias for :meth:`save`."""

        await self.save(caller, memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        return await self._repository.get(caller, memory_id)

    async def search(self, caller: MemoryOwner, query: MemoryQuery) -> tuple[Memory, ...]:
        _require_same_owner(caller, query.owner)
        return await self._repository.search(query)

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        await self._repository.delete(caller, memory_id)

    async def forget(self, caller: MemoryOwner, memory_id: str) -> None:
        await self.delete(caller, memory_id)


def _authorize_write(caller: MemoryOwner, memory: Memory) -> None:
    if memory.scope is MemoryScope.GLOBAL:
        raise MemoryScopeError("global memory writes require an administrative capability")
    if caller.tenant_id != memory.owner.tenant_id or not visible_to(
        memory.scope, memory.owner, caller
    ):
        raise MemoryScopeError("caller is not authorized to write memory for this owner")


def _require_same_owner(caller: MemoryOwner, requested: MemoryOwner) -> None:
    if caller != requested:
        raise MemoryScopeError("memory query owner must match the authenticated caller")
