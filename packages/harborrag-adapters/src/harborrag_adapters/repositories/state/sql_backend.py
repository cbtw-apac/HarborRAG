from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.backends.sqlalchemy import (
    SQLAlchemyDBClient,
)
from harborrag_adapters.repositories.errors import (
    StorageErrorContext,
)
from harborrag_adapters.repositories.state.base import (
    HarborStateBackend,
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

from .sql_checkpoint_store import SQLCheckpointStore
from .sql_lease_store import SQLLeaseStore
from .sql_schema import _METADATA
from .sql_state_store import SQLStateStore


class SQLStateBackend(HarborStateBackend):
    """Composes operational state, checkpoint, and lease stores over SQLAlchemy."""

    def __init__(
        self,
        *,
        client: SQLAlchemyDBClient,
        instance_name: str,
        create_schema: bool,
        telemetry: StorageTelemetryHook | None = None,
    ) -> None:
        self.client = client
        self._instance_name = instance_name
        self._create_schema = create_schema
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.STATE,
            backend=client.backend,
        )
        self.state = SQLStateStore(self)
        self.checkpoints = SQLCheckpointStore(self)
        self.leases = SQLLeaseStore(self)

    async def connect(self) -> None:
        await self.client.connect()
        if self._create_schema:
            await self._initialize_schema()

    async def _initialize_schema(self) -> None:
        await self.client.create_schema(_METADATA)

    async def close(self) -> None:
        await self.client.close()

    async def health(self) -> RepositoryHealth:
        try:
            await self.client.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - integration behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.STATE,
            backend=self.client.backend,
            instance_name=self._instance_name,
            status=status,
            details=details,
        )

    def error_context(
        self,
        operation: str,
        context: StorageOperationContext,
        resource: str,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.STATE,
            backend=self.client.backend,
            instance_name=self._instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=resource,
        )
