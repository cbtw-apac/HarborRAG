from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from harborrag_adapters.repositories.cache.base import HarborLockManager
from harborrag_adapters.repositories.cache.memory.state import MemoryCacheState
from harborrag_adapters.repositories.errors import (
    HarborStorageLeaseError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.telemetry import traced_repository_operation
from harborrag_core.schemas.cache import LockHandle
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext


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
