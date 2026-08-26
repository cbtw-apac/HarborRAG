"""Redis-backed working memory, layered on the existing tenant-scoped cache store.

Satisfies ``harborrag_memory.tiers.working.WorkingMemoryStore`` structurally
(Protocol conformance needs no import of that package) by delegating to
``HarborCacheStore`` -- the same Redis client and key-scoping every other
cache consumer in this codebase already uses, rather than a bespoke
connection.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from harborrag_adapters.repositories.cache.base import HarborCacheStore
from harborrag_core.ports.memory import MemoryOwner
from harborrag_core.storage import StorageOperationContext


class RedisWorkingMemoryStore:
    """Per-run scratch state, TTL'd through the shared Redis cache backend."""

    def __init__(self, cache: HarborCacheStore) -> None:
        self._cache = cache

    async def get(self, owner: MemoryOwner) -> dict[str, Any] | None:
        value = await self._cache.get(_key(owner), context=_context(owner, "working_memory_get"))
        return value if isinstance(value, dict) else None

    async def put(self, owner: MemoryOwner, state: dict[str, Any], *, ttl_seconds: int) -> None:
        await self._cache.set(
            _key(owner),
            state,
            ttl=timedelta(seconds=ttl_seconds),
            tags=None,
            context=_context(owner, "working_memory_put"),
        )

    async def delete(self, owner: MemoryOwner) -> None:
        await self._cache.delete(_key(owner), context=_context(owner, "working_memory_delete"))


def _key(owner: MemoryOwner) -> str:
    return f"working_memory:{owner.principal_id}:{owner.session_id}:{owner.run_id}"


def _context(owner: MemoryOwner, operation_kind: str) -> StorageOperationContext:
    return StorageOperationContext.system(owner.tenant_id, operation_kind=operation_kind)


__all__ = ["RedisWorkingMemoryStore"]
