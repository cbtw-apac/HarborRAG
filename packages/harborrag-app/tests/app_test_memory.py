"""Long-term memory doubles for the shared application-service test fixture.

``FakeMemoryStore`` applies the real ``visible_to`` rule rather than a
shortcut, so a route test that asks for another user's memory fails for the
same reason production would: the row is simply not visible to that owner.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from harborrag_core.base import utc_now
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    modifiable_by,
    visible_to,
)


def _expired(memory: Memory) -> bool:
    return memory.expires_at is not None and memory.expires_at <= utc_now()


class FakeMemoryStore:
    """An in-memory ``MemoryRepository`` with production visibility semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, Memory] = {}

    async def save(self, memory: Memory) -> None:
        self.rows[memory.memory_id] = memory

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        memory = self.rows.get(memory_id)
        if memory is None or _expired(memory) or not visible_to(memory.scope, memory.owner, caller):
            return None
        return memory

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        scopes = query.scopes or tuple(MemoryScope)

        def matches(memory: Memory) -> bool:
            if (
                query.source_session_id is not None
                and memory.source_session_id != query.source_session_id
            ):
                return False
            # Mirrors SqlMemoryRepository: erasure matches the row's stored
            # tenant/user, reads match caller visibility.
            if query.stored_by_owner:
                return (
                    query.owner.user_id is not None
                    and memory.owner.tenant_id == query.owner.tenant_id
                    and memory.owner.user_id == query.owner.user_id
                    and (
                        query.owner.user_id != DEFAULT_USER
                        or query.owner.principal_id is None
                        or memory.owner.principal_id == query.owner.principal_id
                    )
                )
            # Reads never see expired rows; erasure must reach them.
            return not _expired(memory) and visible_to(memory.scope, memory.owner, query.owner)

        found = [
            memory for memory in self.rows.values() if memory.scope in scopes and matches(memory)
        ]
        found.sort(key=lambda memory: (-memory.importance, memory.memory_id))
        return tuple(found[: query.limit])

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        memory = self.rows.get(memory_id)
        if memory is not None and modifiable_by(memory.scope, memory.owner, caller):
            del self.rows[memory_id]

    async def list_expired(
        self, *, cutoffs: Mapping[MemoryScope, datetime], now: datetime, limit: int
    ) -> tuple[Memory, ...]:
        if not cutoffs:
            return ()

        def expired(memory: Memory) -> bool:
            if memory.expires_at is not None and memory.expires_at <= now:
                return True
            cutoff = cutoffs.get(memory.scope)
            return cutoff is not None and memory.updated_at < cutoff

        found = sorted(
            (memory for memory in self.rows.values() if expired(memory)),
            key=lambda memory: (memory.updated_at, memory.memory_id),
        )
        return tuple(found[:limit])


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
    user_id: str = DEFAULT_USER,
    principal_id: str = "dev",
    session_id: str | None = None,
    project_id: str | None = None,
    scope: MemoryScope = MemoryScope.USER,
    content: str = "prefers metric units",
    importance: float = 0.5,
    source_session_id: str | None = None,
    source_message_ids: tuple[str, ...] = (),
    updated_at: datetime | None = None,
) -> Memory:
    """One stored memory for a route or erasure test."""

    stamp = updated_at or utc_now()
    return Memory(
        memory_id=memory_id,
        scope=scope,
        memory_type=MemoryType.PREFERENCE,
        owner=MemoryOwner(
            tenant_id=tenant_id,
            user_id=user_id,
            principal_id=principal_id,
            session_id=session_id,
            project_id=project_id,
        ),
        content=content,
        importance=importance,
        created_at=stamp,
        updated_at=stamp,
        valid_from=stamp,
        source_session_id=source_session_id,
        source_message_ids=source_message_ids,
    )


__all__ = ["FakeMemoryIndex", "FakeMemoryStore", "memory"]
