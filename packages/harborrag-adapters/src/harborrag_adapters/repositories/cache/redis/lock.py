from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from harborrag_core.schemas.cache import LockHandle
from harborrag_core.schemas.storage import StorageOperationContext

from harborrag_adapters.repositories.cache.base import HarborLockManager
from harborrag_adapters.repositories.cache.redis.scripts import (
    ACQUIRE_LOCK,
    RELEASE_LOCK,
    RENEW_LOCK,
)
from harborrag_adapters.repositories.errors import HarborStorageLeaseError
from harborrag_adapters.repositories.telemetry import traced_repository_operation

if TYPE_CHECKING:
    from harborrag_adapters.repositories.cache.redis.repository import (
        RedisCacheBackend,
    )


class RedisLockManager(HarborLockManager):
    """Coordinates Redis lease locks using ownership and monotonic fencing tokens."""

    def __init__(self, repositories: RedisCacheBackend) -> None:
        self._repositories = repositories
        self._telemetry = repositories._telemetry

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
        lock_key = self._repositories.lock_key(context, key)
        owner_token = str(uuid4())
        duration_ms = int(lease_duration.total_seconds() * 1000)
        deadline = asyncio.get_running_loop().time() + (
            wait_timeout.total_seconds() if wait_timeout else 0
        )
        while True:
            fencing = int(
                await self._repositories.client.eval(
                    ACQUIRE_LOCK,
                    2,
                    lock_key,
                    self._repositories.fencing_key(context, key),
                    owner_token,
                    duration_ms,
                )
            )
            if fencing:
                return LockHandle(
                    key=lock_key,
                    owner_token=owner_token,
                    fencing_token=fencing,
                    expires_at=datetime.now(UTC) + lease_duration,
                )
            if wait_timeout is None or asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.05)

    @traced_repository_operation("lock_renew")
    async def renew(
        self,
        handle: LockHandle,
        *,
        lease_duration: timedelta,
        context: StorageOperationContext,
    ) -> LockHandle:
        self._validate_duration(lease_duration, "lease_duration")
        duration_ms = int(lease_duration.total_seconds() * 1000)
        renewed = await self._repositories.client.eval(
            RENEW_LOCK,
            1,
            handle.key,
            handle.owner_token,
            duration_ms,
        )
        if not renewed:
            raise HarborStorageLeaseError(
                f"lock {handle.key!r} is not owned or has expired",
                context=self._repositories.error_context("lock_renew", handle.key),
            )
        return handle.model_copy(update={"expires_at": datetime.now(UTC) + lease_duration})

    @traced_repository_operation("lock_release")
    async def release(
        self,
        handle: LockHandle,
        *,
        context: StorageOperationContext,
    ) -> bool:
        released = await self._repositories.client.eval(
            RELEASE_LOCK,
            1,
            handle.key,
            handle.owner_token,
        )
        return bool(released)

    @staticmethod
    def _validate_duration(value: timedelta, name: str) -> None:
        if value <= timedelta(0):
            raise ValueError(f"{name} must be positive")
