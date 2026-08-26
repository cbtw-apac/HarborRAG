from __future__ import annotations

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.storage import HealthStatus, RepositoryHealth, StorageFamily

from .document_versions import DocumentVersionRepository
from .publication import DocumentVersionPublisher
from .reindex import ReindexJobRepository
from .reliability import IngestionReliabilityRepository
from .schema import METADATA
from .source_scans import SourceScanRepository
from .task_events import TaskEventRepository
from .tasks import IngestionTaskRepository


class IngestionControlPlaneDatabase:
    """Own the Postgres authority used by ingestion and retrieval validation."""

    def __init__(
        self,
        client: SQLAlchemyDBClient,
        *,
        create_schema: bool = False,
        owns_client: bool = True,
    ) -> None:
        self._client = client
        self._create_schema = create_schema
        self._owns_client = owns_client
        self.source_scans = SourceScanRepository(client)
        self.document_versions = DocumentVersionRepository(client)
        self.publisher = DocumentVersionPublisher(client)
        self.reliability = IngestionReliabilityRepository(client)
        self.reindex = ReindexJobRepository(client)
        self.tasks = IngestionTaskRepository(client)
        self.task_events = TaskEventRepository(client)

    async def __aenter__(self) -> IngestionControlPlaneDatabase:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()

    async def connect(self) -> None:
        await self._client.connect()
        if self._create_schema:
            await self.provision()

    async def provision(self) -> None:
        """Provision idempotent control-plane tables and constraints."""

        await self._client.create_schema(METADATA)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def health(self) -> RepositoryHealth:
        try:
            await self._client.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, object] = {}
        except Exception as exc:  # pragma: no cover - live dependency behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.DATABASE,
            backend=self._client.backend,
            instance_name="ingestion-control-plane",
            status=status,
            details=details,
        )
