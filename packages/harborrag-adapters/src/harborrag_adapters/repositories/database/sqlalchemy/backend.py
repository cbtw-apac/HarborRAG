from __future__ import annotations

from typing import Any

from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
)

from harborrag_adapters.repositories.database.base import (
    HarborDatabaseBackend,
    HarborUnitOfWork,
    HarborUnitOfWorkFactory,
)
from harborrag_adapters.repositories.shared.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
)

from .chunks import SQLChunkRepository, SQLOutboxRepository
from .documents import SQLDocumentRepository
from .schema import METADATA


class SQLAlchemyUnitOfWork(HarborUnitOfWork):
    """Own one SQLAlchemy transaction shared by cohesive repositories."""

    def __init__(
        self,
        client: SQLAlchemyDBClient,
        instance_name: str,
        telemetry: RepositoryTelemetry,
    ) -> None:
        self._client = client
        self._instance_name = instance_name
        self._telemetry = telemetry
        self._session: Any = None
        self._committed = False

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        self._session = self._client.sessions()
        await self._session.begin()
        self.documents = SQLDocumentRepository(
            self._session,
            self._client.backend,
            self._instance_name,
            self._telemetry,
        )
        self.chunks = SQLChunkRepository(
            self._session,
            self._client.backend,
            self._instance_name,
            self._telemetry,
        )
        self.outbox = SQLOutboxRepository(self._session, self._telemetry)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        try:
            if not self._committed:
                await self._session.rollback()
        finally:
            await self._session.close()

    async def commit(self) -> None:
        await self._session.commit()
        self._committed = True

    async def rollback(self) -> None:
        await self._session.rollback()
        self._committed = True


class SQLAlchemyUnitOfWorkFactory(HarborUnitOfWorkFactory):
    """Create isolated units of work over one shared database client."""

    def __init__(
        self,
        client: SQLAlchemyDBClient,
        instance_name: str,
        telemetry: RepositoryTelemetry,
    ) -> None:
        self._client = client
        self._instance_name = instance_name
        self._telemetry = telemetry

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(self._client, self._instance_name, self._telemetry)


class SQLAlchemyDatabaseBackend(HarborDatabaseBackend):
    """Compose document, chunk, and outbox persistence over SQLAlchemy."""

    def __init__(
        self,
        *,
        client: SQLAlchemyDBClient,
        instance_name: str,
        create_schema: bool,
        telemetry: StorageTelemetryHook | None = None,
    ) -> None:
        self._database = client
        self._instance_name = instance_name
        self._create_schema = create_schema
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.DATABASE,
            backend=client.backend,
        )
        self.unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(
            client,
            instance_name,
            self._telemetry,
        )

    async def connect(self) -> None:
        await self._database.connect()
        if self._create_schema:
            await self._database.create_schema(METADATA)

    async def close(self) -> None:
        await self._database.close()

    async def health(self) -> RepositoryHealth:
        try:
            await self._database.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - integration behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.DATABASE,
            backend=self._database.backend,
            instance_name=self._instance_name,
            status=status,
            details=details,
        )
