from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from harborrag_adapters.repositories.cache.base import (
    HarborCacheStore,
)
from harborrag_adapters.repositories.cache.memory.state import MemoryCacheState
from harborrag_adapters.repositories.telemetry import traced_repository_operation
from harborrag_core.schemas.cache import CacheEntry
from harborrag_core.storage import StorageOperationContext


class MemoryCacheRepository(HarborCacheStore):
    """Provides process-local TTL caching, counters, tags, and compare-and-set."""

    def __init__(self, state: MemoryCacheState) -> None:
        self._state = state
        self._telemetry = state.telemetry

    @traced_repository_operation("get")
    async def get(self, key: str, *, context: StorageOperationContext) -> Any | None:
        scoped = self._state.key(context, key)
        async with self._state.mutex:
            entry = self._live_entry(scoped, context)
            return deepcopy(entry.value) if entry else None

    @traced_repository_operation("get_many")
    async def get_many(
        self,
        keys: list[str],
        *,
        context: StorageOperationContext,
    ) -> dict[str, Any]:
        values = await asyncio.gather(*(self.get(key, context=context) for key in keys))
        return {key: value for key, value in zip(keys, values, strict=True) if value is not None}

    @traced_repository_operation("set")
    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: timedelta | None,
        tags: set[str] | None,
        context: StorageOperationContext,
    ) -> None:
        scoped = self._state.key(context, key)
        async with self._state.mutex:
            previous = self._state.entries.get(scoped)
            if previous:
                self._remove(scoped, previous, context)
            if ttl is not None and ttl <= timedelta(0):
                return
            entry = CacheEntry(
                key=scoped,
                value=deepcopy(value),
                version=previous.version + 1 if previous else 1,
                tags=tags or set(),
                expires_at=datetime.now(UTC) + ttl if ttl else None,
            )
            self._state.entries[scoped] = entry
            for tag in entry.tags:
                self._state.tags.setdefault(self._state.tag(context, tag), set()).add(scoped)

    @traced_repository_operation("compare_and_set")
    async def compare_and_set(
        self,
        key: str,
        expected: Any,
        value: Any,
        *,
        ttl: timedelta | None,
        context: StorageOperationContext,
    ) -> bool:
        scoped = self._state.key(context, key)
        async with self._state.mutex:
            entry = self._live_entry(scoped, context)
            actual = entry.value if entry else None
            if actual != expected:
                return False
            if entry:
                self._remove(scoped, entry, context)
            if ttl is not None and ttl <= timedelta(0):
                return True
            preserved_tags = set(entry.tags) if entry else set()
            replacement = CacheEntry(
                key=scoped,
                value=deepcopy(value),
                version=entry.version + 1 if entry else 1,
                tags=preserved_tags,
                expires_at=datetime.now(UTC) + ttl if ttl else None,
            )
            self._state.entries[scoped] = replacement
            for tag in preserved_tags:
                self._state.tags.setdefault(self._state.tag(context, tag), set()).add(scoped)
            return True

    @traced_repository_operation("increment")
    async def increment(
        self,
        key: str,
        amount: int,
        *,
        ttl: timedelta | None,
        context: StorageOperationContext,
    ) -> int:
        scoped = self._state.key(context, key)
        async with self._state.mutex:
            entry = self._live_entry(scoped, context)
            current = entry.value if entry else 0
            if not isinstance(current, int) or isinstance(current, bool):
                raise TypeError("cache increment requires an integer value")
            result = current + amount
            if ttl is not None and ttl <= timedelta(0):
                if entry:
                    self._remove(scoped, entry, context)
                return result
            self._state.entries[scoped] = CacheEntry(
                key=scoped,
                value=result,
                version=entry.version + 1 if entry else 1,
                tags=entry.tags if entry else set(),
                expires_at=(
                    datetime.now(UTC) + ttl if ttl else entry.expires_at if entry else None
                ),
            )
            return result

    @traced_repository_operation("delete")
    async def delete(self, key: str, *, context: StorageOperationContext) -> bool:
        scoped = self._state.key(context, key)
        async with self._state.mutex:
            entry = self._state.entries.get(scoped)
            if entry is None:
                return False
            self._remove(scoped, entry, context)
            return True

    @traced_repository_operation("invalidate_tag")
    async def invalidate_tag(
        self,
        tag: str,
        *,
        context: StorageOperationContext,
    ) -> int:
        scoped_tag = self._state.tag(context, tag)
        async with self._state.mutex:
            keys = self._state.tags.pop(scoped_tag, set())
            removed = 0
            for key in tuple(keys):
                entry = self._state.entries.get(key)
                if entry:
                    self._remove(key, entry, context)
                    removed += 1
            return removed

    def _live_entry(
        self,
        scoped: str,
        context: StorageOperationContext | None = None,
    ) -> CacheEntry | None:
        entry = self._state.entries.get(scoped)
        if entry is None:
            return None
        if entry.expires_at is None or entry.expires_at > datetime.now(UTC):
            return entry
        if context is None:
            self._state.entries.pop(scoped, None)
        else:
            self._remove(scoped, entry, context)
        return None

    def _remove(
        self,
        scoped: str,
        entry: CacheEntry,
        context: StorageOperationContext,
    ) -> None:
        self._state.entries.pop(scoped, None)
        for tag in entry.tags:
            scoped_tag = self._state.tag(context, tag)
            members = self._state.tags.get(scoped_tag)
            if members is None:
                continue
            members.discard(scoped)
            if not members:
                self._state.tags.pop(scoped_tag, None)
