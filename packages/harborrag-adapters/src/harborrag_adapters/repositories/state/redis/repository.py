from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.backends.redis import RedisDBClient
from harborrag_adapters.repositories.errors import StorageErrorContext
from harborrag_adapters.repositories.policies.key_encoding import escape_key_part
from harborrag_adapters.repositories.state.base import HarborStateBackend
from harborrag_adapters.repositories.state.redis.config import RedisStateConfig
from harborrag_adapters.repositories.state.redis.stores import (
    RedisCheckpointStore,
    RedisLeaseStore,
    RedisStateStore,
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


class RedisStateBackend(HarborStateBackend):
    """Composes state, checkpoint, and lease repositories around one Redis client."""

    def __init__(
        self,
        config: RedisStateConfig,
        telemetry: StorageTelemetryHook | None = None,
        *,
        client: RedisDBClient | None = None,
    ) -> None:
        self._config = config
        self._database = client or RedisDBClient(
            url=config.url.get_secret_value(),
            connect_timeout_seconds=config.connect_timeout_seconds,
            operation_timeout_seconds=config.operation_timeout_seconds,
        )
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.STATE,
            backend="redis",
        )
        self.state = RedisStateStore(self)
        self.checkpoints = RedisCheckpointStore(self)
        self.leases = RedisLeaseStore(self)

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
                family=StorageFamily.STATE,
                backend="redis",
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
            family=StorageFamily.STATE,
            backend="redis",
            instance_name=self._config.instance_name,
            status=status,
            details=details,
        )

    def key(self, context: StorageOperationContext, *parts: str) -> str:
        safe_parts = [escape_key_part(part) for part in parts]
        return ":".join([self._config.key_prefix, str(context.tenant_id), *safe_parts])

    def error_context(
        self,
        operation: str,
        context: StorageOperationContext,
        resource: str,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.STATE,
            backend="redis",
            instance_name=self._config.instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=resource,
        )
