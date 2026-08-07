"""Long-term memory facade.

The repository contract is owned by ``harborrag-core`` and implemented by the
SQL adapters. This wrapper gives the memory package a clear orchestration layer
without becoming a database abstraction itself.
"""

from __future__ import annotations

from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery, MemoryRepository


class LongTermMemory:
    """Facade over the canonical long-term memory repository."""

    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    async def save(self, memory: Memory) -> None:
        await self._repository.save(memory)

    async def remember(self, memory: Memory) -> None:
        await self.save(memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        return await self._repository.get(caller, memory_id)

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        return await self._repository.search(query)

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        await self._repository.delete(caller, memory_id)

    async def forget(self, caller: MemoryOwner, memory_id: str) -> None:
        await self.delete(caller, memory_id)


__all__ = ["LongTermMemory"]