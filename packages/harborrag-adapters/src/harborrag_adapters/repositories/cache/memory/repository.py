from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from harborrag_core.schemas.cache import CacheEntry, LockHandle
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)

from harborrag_adapters.repositories.cache.base import (
    HarborCacheBackend,
    HarborCacheStore,
    HarborLockManager,
)
from harborrag_adapters.repositories.errors import (
    HarborStorageLeaseError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.shared.keys import escape_key_part
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)


@dataclass(slots=True)
class MemoryCacheState:
    """Process-local cache data owned by exactly one backend instance."""

    instance_name: str
    key_prefix: str
    telemetry: RepositoryTelemetry
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    tags: dict[str, set[str]] = field(default_factory=dict)
    locks: dict[str, LockHandle] = field(default_factory=dict)
    fencing_tokens: dict[str, int] = field(default_factory=dict)
    mutex: asyncio.Lock = field(default_factory=asyncio.Lock)
    connected: bool = False

    def key(self, context: StorageOperationContext, value: str) -> str:
        return f"{self.key_prefix}:{context.tenant_id}:{escape_key_part(value)}"

    @staticmethod
    def tag(context: StorageOperationContext, value: str) -> str:
        return f"{context.tenant_id}:{escape_key_part(value)}"


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


class MemoryLockManager(HarborLockManager):
    """Coordinates process-local leases with ownership and fencing tokens."""

    def __init__(self, state: MemoryCacheState) -> None:
        self._state = state
        self._telemetry = state.telemetry

    @traced_repository_operation("lock_acquire")
    async def acquire(
        self,
        key: str,
        *,
        lease_duration: timedelta,
        wait_timeout: timedelta | None,
        context: StorageOperationContext,
    ) -> LockHandle | None:
        self._validate_duration(lease_duration, "lease_duration")
        if wait_timeout is not None and wait_timeout < timedelta(0):
            raise ValueError("wait_timeout must not be negative")
        scoped = self._state.key(context, f"lock:{key}")
        deadline = datetime.now(UTC) + wait_timeout if wait_timeout else None
        while True:
            now = datetime.now(UTC)
            async with self._state.mutex:
                current = self._state.locks.get(scoped)
                if current is None or current.expires_at <= now:
                    fencing = self._state.fencing_tokens.get(scoped, 0) + 1
                    self._state.fencing_tokens[scoped] = fencing
                    handle = LockHandle(
                        key=scoped,
                        owner_token=str(uuid4()),
                        fencing_token=fencing,
                        expires_at=now + lease_duration,
                    )
                    self._state.locks[scoped] = handle
                    return handle
            if deadline is None or now >= deadline:
                return None
            await asyncio.sleep(min(0.05, max(0.0, (deadline - now).total_seconds())))

    @traced_repository_operation("lock_renew")
    async def renew(
        self,
        handle: LockHandle,
        *,
        lease_duration: timedelta,
        context: StorageOperationContext,
    ) -> LockHandle:
        self._validate_duration(lease_duration, "lease_duration")
        now = datetime.now(UTC)
        async with self._state.mutex:
            current = self._state.locks.get(handle.key)
            if (
                current is None
                or current.owner_token != handle.owner_token
                or current.expires_at <= now
            ):
                raise HarborStorageLeaseError(
                    f"lock {handle.key!r} is not owned or has expired",
                    context=self._error_context("lock_renew", handle.key),
                )
            renewed = current.model_copy(update={"expires_at": now + lease_duration})
            self._state.locks[handle.key] = renewed
            return renewed

    @traced_repository_operation("lock_release")
    async def release(
        self,
        handle: LockHandle,
        *,
        context: StorageOperationContext,
    ) -> bool:
        async with self._state.mutex:
            current = self._state.locks.get(handle.key)
            if current is None or current.owner_token != handle.owner_token:
                return False
            self._state.locks.pop(handle.key)
            return True

    @staticmethod
    def _validate_duration(value: timedelta, name: str) -> None:
        if value <= timedelta(0):
            raise ValueError(f"{name} must be positive")

    def _error_context(self, operation: str, resource: str) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.CACHE,
            backend="memory",
            instance_name=self._state.instance_name,
            operation=operation,
            resource_name=resource,
        )


class MemoryCacheBackend(HarborCacheBackend):
    """Owns a native process-local cache without Redis emulation."""

    def __init__(
        self,
        *,
        instance_name: str = "default",
        key_prefix: str = "harborrag:v1",
        telemetry: StorageTelemetryHook | None = None,
    ) -> None:
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.CACHE,
            backend="memory",
        )
        self._state = MemoryCacheState(
            instance_name=instance_name,
            key_prefix=key_prefix,
            telemetry=self._telemetry,
        )
        self.cache = MemoryCacheRepository(self._state)
        self.locks = MemoryLockManager(self._state)

    async def connect(self) -> None:
        self._state.connected = True

    async def close(self) -> None:
        self._state.connected = False

    async def health(self) -> RepositoryHealth:
        return RepositoryHealth(
            family=StorageFamily.CACHE,
            backend="memory",
            instance_name=self._state.instance_name,
            status=HealthStatus.HEALTHY if self._state.connected else HealthStatus.UNKNOWN,
            details={
                "entries": len(self._state.entries),
                "locks": len(self._state.locks),
                "distributed": False,
            },
        )
