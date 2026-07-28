from __future__ import annotations

from harborrag_adapters.repositories.cache.base import HarborCacheBackend
from harborrag_adapters.repositories.cache.memory.locking import MemoryLockManager
from harborrag_adapters.repositories.cache.memory.repository import (
    MemoryCacheRepository,
)
from harborrag_adapters.repositories.cache.memory.state import MemoryCacheState
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
)
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
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
            status=(HealthStatus.HEALTHY if self._state.connected else HealthStatus.UNKNOWN),
            details={
                "entries": len(self._state.entries),
                "locks": len(self._state.locks),
                "distributed": False,
            },
        )
