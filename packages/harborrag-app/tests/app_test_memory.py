"""Long-term memory doubles for the shared application-service test fixture.

``FakeMemoryStore`` applies the real ``visible_to`` rule rather than a
shortcut, so a route test that asks for another user's memory fails for the
same reason production would: the row is simply not visible to that owner.
"""

from __future__ import annotations

from harborrag_core.base import utc_now
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    visible_to,
)


class FakeMemoryStore:
    """An in-memory ``MemoryRepository`` with production visibility semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, Memory] = {}

    async def save(self, memory: Memory) -> None:
        self.rows[memory.memory_id] = memory

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        memory = self.rows.get(memory_id)
        if memory is None or not visible_to(memory.scope, memory.owner, caller):
            return None
        return memory

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        scopes = query.scopes or tuple(MemoryScope)
        found = [
            memory
            for memory in self.rows.values()
            if memory.scope in scopes and visible_to(memory.scope, memory.owner, query.owner)
        ]
        found.sort(key=lambda memory: (-memory.importance, memory.memory_id))
        return tuple(found[: query.limit])

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        memory = self.rows.get(memory_id)
        if memory is not None and visible_to(memory.scope, memory.owner, caller):
            del self.rows[memory_id]


class FakeMemoryIndex:
    """Records the vector points an erasure asked it to drop."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self.indexed: list[str] = []
        self.failure = failure

    async def index_memory(self, memory: Memory, *, embedding: object = None) -> None:
        del embedding
        self.indexed.append(memory.memory_id)

    async def search_memories(self, query: MemoryQuery, *, embedding: object = None) -> tuple[()]:
        del query, embedding
        return ()

    async def delete_memory(self, owner: MemoryOwner, memory_id: str) -> None:
        del owner
        if self.failure is not None:
            raise self.failure
        self.deleted.append(memory_id)


def memory(  # noqa: PLR0913 - a record builder, one argument per stored field
    memory_id: str,
    *,
    tenant_id: str = "DEFAULT",
    user_id: str = "dev",
    principal_id: str = "dev",
    session_id: str | None = None,
    scope: MemoryScope = MemoryScope.USER,
    content: str = "prefers metric units",
    importance: float = 0.5,
    source_session_id: str | None = None,
    source_message_ids: tuple[str, ...] = (),
) -> Memory:
    """One stored memory for a route or erasure test."""

    return Memory(
        memory_id=memory_id,
        scope=scope,
        memory_type=MemoryType.PREFERENCE,
        owner=MemoryOwner(
            tenant_id=tenant_id,
            user_id=user_id,
            principal_id=principal_id,
            session_id=session_id,
        ),
        content=content,
        importance=importance,
        created_at=utc_now(),
        updated_at=utc_now(),
        valid_from=utc_now(),
        source_session_id=source_session_id,
        source_message_ids=source_message_ids,
    )


__all__ = ["FakeMemoryIndex", "FakeMemoryStore", "memory"]
