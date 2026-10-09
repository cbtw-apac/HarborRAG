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
from harborrag_adapters.repositories.policies.key_encoding import escape_key_part
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


# Every owner field except tenant_id (which the storage context carries), in a
# fixed order. InMemoryWorkingMemoryStore keys on the whole MemoryOwner, so
# Redis must too -- one credential fronting several users must not share state.
_KEY_FIELDS = ("project_id", "user_id", "principal_id", "session_id", "run_id")
# escape_key_part rewrites "%" to "%25", so no escaped string can equal this.
_ABSENT = "%00"
_KEY_VERSION = "v2"


def _key_part(value: str | None) -> str:
    return _ABSENT if value is None else escape_key_part(value)


def _key(owner: MemoryOwner) -> str:
    """One key per owner: each part escaped on its own, absent fields distinct.

    ``v2``: keys written by the previous format (parts joined before
    escaping, ``None`` as the string "None") are no longer read. Working
    memory is TTL-bound scratch state, so those keys simply expire.
    """

    parts = (_key_part(getattr(owner, name)) for name in _KEY_FIELDS)
    return ":".join(("working_memory", _KEY_VERSION, *parts))


def _context(owner: MemoryOwner, operation_kind: str) -> StorageOperationContext:
    return StorageOperationContext.system(owner.tenant_id, operation_kind=operation_kind)


__all__ = ["RedisWorkingMemoryStore"]
