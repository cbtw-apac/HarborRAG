from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.cache.base import HarborCacheBackend
from harborrag_adapters.repositories.cache.redis.config import RedisCacheConfig
from harborrag_adapters.repositories.cache.redis.lock import RedisLockManager
from harborrag_adapters.repositories.cache.redis.store import RedisCacheStore
from harborrag_adapters.repositories.errors import StorageErrorContext
from harborrag_adapters.repositories.shared.keys import escape_key_part
from harborrag_adapters.repositories.shared.redis import (
    RedisClientProtocol,
    RedisDBClient,
)
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
)
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)


class RedisCacheBackend(HarborCacheBackend):
    """Composes cache and lock repositories around one shared Redis database client."""

    def __init__(
        self,
        config: RedisCacheConfig,
        telemetry: StorageTelemetryHook | None = None,
        *,
        client: RedisClientProtocol | None = None,
    ) -> None:
        self._config = config
        self._database = client or RedisDBClient(
            url=config.url.get_secret_value(),
            connect_timeout_seconds=config.connect_timeout_seconds,
            operation_timeout_seconds=config.operation_timeout_seconds,
        )
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.CACHE,
            backend="redis",
        )
        self.cache = RedisCacheStore(self)
        self.locks = RedisLockManager(self)

    @property
    def client(self) -> Any:
        return self._database.raw

    async def connect(self) -> None:
        await self._database.connect()

    async def close(self) -> None:
        await self._database.close()

    async def health(self) -> RepositoryHealth:
        if not self._database.is_connected:
            return RepositoryHealth(
                family=StorageFamily.CACHE,
                backend=self._database.backend,
                instance_name=self._config.instance_name,
                status=HealthStatus.UNKNOWN,
            )
        try:
            await self._database.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - integration behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.CACHE,
            backend=self._database.backend,
            instance_name=self._config.instance_name,
            status=status,
            details=details,
        )

    def value_key(self, context: StorageOperationContext, key: str) -> str:
        return f"{self._config.key_prefix}:{context.tenant_id}:cache:{escape_key_part(key)}"

    def tags_key(self, context: StorageOperationContext, key: str) -> str:
        return f"{self._config.key_prefix}:{context.tenant_id}:cache_tags:{escape_key_part(key)}"

    @staticmethod
    def tags_key_for_value_key(value_key: str) -> str:
        return value_key.replace(":cache:", ":cache_tags:", 1)

    def tag_key(self, context: StorageOperationContext, tag: str) -> str:
        return f"{self._config.key_prefix}:{context.tenant_id}:tag:{escape_key_part(tag)}"

    def lock_key(self, context: StorageOperationContext, key: str) -> str:
        return f"{self._config.key_prefix}:{context.tenant_id}:lock:{escape_key_part(key)}"

    def fencing_key(self, context: StorageOperationContext, key: str) -> str:
        return f"{self._config.key_prefix}:{context.tenant_id}:fence:{escape_key_part(key)}"

    def error_context(self, operation: str, resource: str) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.CACHE,
            backend=self._database.backend,
            instance_name=self._config.instance_name,
            operation=operation,
            resource_name=resource,
        )
