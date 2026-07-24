from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any

from harborrag_adapters.repositories.lifecycle import RepositoryLifecycle
from harborrag_core.schemas.cache import LockHandle
from harborrag_core.schemas.storage import StorageOperationContext


class HarborCacheStore(ABC):
    """Defines tenant-scoped cache semantics independently of provider lifecycle."""

    @abstractmethod
    async def get(self, key: str, *, context: StorageOperationContext) -> Any | None:
        """Load a cached value."""

    @abstractmethod
    async def get_many(
        self,
        keys: list[str],
        *,
        context: StorageOperationContext,
    ) -> dict[str, Any]:
        """Load all existing values for the requested keys."""

    @abstractmethod
    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: timedelta | None,
        tags: set[str] | None,
        context: StorageOperationContext,
    ) -> None:
        """Store a value with optional expiration and invalidation tags."""

    @abstractmethod
    async def compare_and_set(
        self,
        key: str,
        expected: Any,
        value: Any,
        *,
        ttl: timedelta | None,
        context: StorageOperationContext,
    ) -> bool:
        """Atomically replace a value only when it matches the expectation."""

    @abstractmethod
    async def increment(
        self,
        key: str,
        amount: int,
        *,
        ttl: timedelta | None,
        context: StorageOperationContext,
    ) -> int:
        """Atomically increment an integer value."""

    @abstractmethod
    async def delete(self, key: str, *, context: StorageOperationContext) -> bool:
        """Delete one cached value."""

    @abstractmethod
    async def invalidate_tag(
        self,
        tag: str,
        *,
        context: StorageOperationContext,
    ) -> int:
        """Delete all values associated with one invalidation tag."""


class HarborLockManager(ABC):
    """Defines token-owned leased locks with monotonic fencing values."""

    @abstractmethod
    async def acquire(
        self,
        key: str,
        *,
        lease_duration: timedelta,
        wait_timeout: timedelta | None,
        context: StorageOperationContext,
    ) -> LockHandle | None:
        """Acquire a lock or return no handle before the wait timeout."""

    @abstractmethod
    async def renew(
        self,
        handle: LockHandle,
        *,
        lease_duration: timedelta,
        context: StorageOperationContext,
    ) -> LockHandle:
        """Renew a lock only for its current owner token."""

    @abstractmethod
    async def release(
        self,
        handle: LockHandle,
        *,
        context: StorageOperationContext,
    ) -> bool:
        """Release a lock only for its current owner token."""


class HarborCacheBackend(RepositoryLifecycle):
    """Owns one cache backend lifecycle and its cohesive cache products."""

    cache: HarborCacheStore
    locks: HarborLockManager
