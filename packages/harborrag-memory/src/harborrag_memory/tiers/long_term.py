"""Long-term memory tier: durable, searchable memories scoped by owner."""

from __future__ import annotations

from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery, MemoryRepository


class LongTermMemory:
    """Facade over the canonical long-term memory repository."""

    def __init__(self, repository: MemoryRepository) -> None:
        # TODO: wire the long-term memory repository dependency.
        pass

    async def save(self, memory: Memory) -> None:
        # TODO: persist the memory via the repository.
        raise NotImplementedError("TODO: implement LongTermMemory.save")

    async def remember(self, memory: Memory) -> None:
        # TODO: alias for save.
        raise NotImplementedError("TODO: implement LongTermMemory.remember")

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        # TODO: fetch a single memory by id, scoped to the caller.
        raise NotImplementedError("TODO: implement LongTermMemory.get")

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        # TODO: search memories matching the query.
        raise NotImplementedError("TODO: implement LongTermMemory.search")

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        # TODO: delete a memory by id, scoped to the caller.
        raise NotImplementedError("TODO: implement LongTermMemory.delete")

    async def forget(self, caller: MemoryOwner, memory_id: str) -> None:
        # TODO: alias for delete.
        raise NotImplementedError("TODO: implement LongTermMemory.forget")