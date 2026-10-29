"""Runtime resource ownership and public composition entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.models.embed import HarborEmbedClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.object_store import ARTIFACT_BUCKET, RAW_BUCKET
from harborrag_core.ingestion import ProcessingProfile
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_core.ports.storage import ObjectStorePort, VectorRepositoryPort
from harborrag_runtime.composition.connection_stores import ConnectionStores
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import RuntimeConnectionError

from .connections import ConnectionHealth, ConnectionResolver, ConnectorPool
from .document.pipeline import DocumentStagePipeline
from .document.service import DocumentReleaseService
from .maintenance.cleanup import ProjectionCleanupService
from .maintenance.reindex import DocumentReindexService
from .maintenance.relation_repair import GraphRelationRepairService
from .maintenance.retention import RetiredVersionPurgeService
from .observability import IngestionTelemetry
from .source.plan import SourcePlanRepository
from .source.service import SourceIngestionService

if TYPE_CHECKING:
    from harborrag_engine.ingestion import ChunkingConfig, ChunkStrategy

    from .document.normalizers import SourceDocumentNormalizerBuilder

logger = logging.getLogger("harborrag.runtime.ingestion.composition")

_FALLBACK_TARGETS = {
    "control": "the control database",
    "object_store": "the object store",
    "vector": "the vector store",
    "graph": "the graph store",
}
# Fallbacks for runtimes built without resource_hints (tests, embedders).
_START_HINTS = {
    "control": "check HARBORRAG_CONTROL_DB_URL",
    "object_store": "check HARBORRAG_OBJECT_STORE_* credentials and that MinIO is running",
    "vector": "start Qdrant or set HARBORRAG_QDRANT_PREFER_GRPC=false to use HTTP",
    "graph": "start FalkorDB",
}


@dataclass(slots=True)
class IngestionRuntime:
    """Own connected providers and production ingestion application services."""

    connector_pool: ConnectorPool
    processing: ProcessingProfile
    control: IngestionControlPlaneDatabase
    documents: DocumentReleaseService
    stages: DocumentStagePipeline
    sources: SourceIngestionService
    relations: GraphRelationRepairService
    cleanup: ProjectionCleanupService
    reindex: DocumentReindexService
    source_plans: SourcePlanRepository
    object_store: ObjectStorePort
    vector_repository: VectorRepositoryPort
    graph_repository: KnowledgeGraphRepositoryPort
    embed_client: HarborEmbedClient
    connector_rate_limiter: ConnectorRateLimiter
    telemetry: IngestionTelemetry
    connection_health: ConnectionHealth = field(default_factory=lambda: ConnectionHealth(None))
    connection_resolver: ConnectionResolver | None = None
    connection_stores: ConnectionStores | None = None
    # None keeps hand-built runtimes (tests, embedders) free of the purge.
    retention: RetiredVersionPurgeService | None = None
    # Safe display strings per resource label; read only on the failure path.
    resource_targets: Mapping[str, str] = field(default_factory=dict)
    # What to check per resource label for the configured providers; same path.
    resource_hints: Mapping[str, str] = field(default_factory=dict)
    _started: bool = field(default=False, init=False)

    async def start(self) -> None:
        """Connect shared infrastructure only; connectors are built per run on first use."""
        if self._started:
            logger.debug("Ingestion runtime start skipped reason=already_started")
            return
        connected: list[Callable[[], Awaitable[None]]] = []
        try:
            await self.telemetry.start()
            connected.append(self.telemetry.close)
            for label, resource in (
                ("control", self.control),
                ("object_store", self.object_store),
                ("vector", self.vector_repository),
                ("graph", self.graph_repository),
            ):
                try:
                    await resource.connect()
                except Exception as error:
                    raise self._unreachable(label, error) from error
                connected.append(resource.close)
            try:
                await self.object_store.ensure_buckets((RAW_BUCKET, ARTIFACT_BUCKET))
            except Exception as error:
                raise self._unreachable("object_store", error) from error
        except BaseException as error:
            logger.error(
                "Ingestion runtime startup failed error_type=%s connected_resources=%d",
                type(error).__name__,
                len(connected),
            )
            await asyncio.gather(
                *(close() for close in reversed(connected)),
                asyncio.to_thread(self.connector_rate_limiter.close),
                self.embed_client.aclose(),
                return_exceptions=True,
            )
            raise
        self._started = True
        logger.info("Ingestion runtime started resources=%d", len(connected))

    def _unreachable(self, label: str, error: Exception) -> RuntimeConnectionError:
        """Authored message only: the driver's text may carry credentials."""
        targets = getattr(self, "resource_targets", None) or {}
        target = targets.get(label) or _FALLBACK_TARGETS[label]
        hints = getattr(self, "resource_hints", None) or {}
        hint = hints.get(label) or _START_HINTS[label]
        logger.error(
            "Ingestion runtime resource unreachable resource=%s error_type=%s",
            label,
            type(error).__name__,
        )
        return RuntimeConnectionError(f"Could not connect to or initialise {target}; {hint}")

    async def close(self) -> None:
        if not self._started:
            await asyncio.gather(
                asyncio.to_thread(self.connector_rate_limiter.close),
                self.embed_client.aclose(),
                self.telemetry.close(),
                self._close_connection_stores(),
            )
            logger.debug("Ingestion runtime closed from unstarted state")
            return
        outcomes = await asyncio.gather(
            self.connector_pool.aclose(),
            self._close_connection_stores(),
            self.graph_repository.close(),
            self.vector_repository.close(),
            self.object_store.close(),
            self.control.close(),
            asyncio.to_thread(self.connector_rate_limiter.close),
            self.embed_client.aclose(),
            self.telemetry.close(),
            return_exceptions=True,
        )
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        if failures:
            logger.error(
                "Ingestion runtime shutdown failed failures=%d error_types=%s",
                len(failures),
                ",".join(sorted({type(error).__name__ for error in failures})),
            )
            # Keep the runtime retryable. Repository/connector close contracts
            # are idempotent, so a later close attempt can converge resources
            # whose first close failed.
            raise BaseExceptionGroup("ingestion runtime shutdown failed", failures)
        self._started = False
        logger.info("Ingestion runtime closed")

    async def connector(
        self,
        *,
        tenant_id: str,
        task_id: str,
        configuration_fingerprint: str,
    ) -> HarborConnector:
        """Return the run's connector, built from the snapshot the run started with.

        Args:
            tenant_id: Tenant of the run.
            task_id: The run; every activity of one run shares its connector.
            configuration_fingerprint: Fingerprint of the run's connection snapshot.

        Returns:
            The connector facade.

        Raises:
            ConnectorConfigurationError: The snapshot is unknown or rejected.
            SecretUnavailableError: A referenced secret cannot be read on this worker.
        """
        return await self.connector_pool.connector(
            tenant_id=tenant_id,
            task_id=task_id,
            configuration_fingerprint=configuration_fingerprint,
        )

    async def _close_connection_stores(self) -> None:
        """Dispose the control-plane engine the connection stores use."""
        if self.connection_stores is not None:
            await self.connection_stores.aclose()


def build_ingestion_runtime(
    settings: RuntimeSettings | None = None,
    *,
    normalizer_builder: SourceDocumentNormalizerBuilder | None = None,
    chunking_config: ChunkingConfig | None = None,
    chunking_strategies: tuple[ChunkStrategy, ...] = (),
) -> IngestionRuntime:
    """Build an ingestion runtime through the production object-graph builder."""

    from .runtime_builder import build_ingestion_runtime as build

    return build(
        settings,
        normalizer_builder=normalizer_builder,
        chunking_config=chunking_config,
        chunking_strategies=chunking_strategies,
    )
