"""Runtime resource ownership and public composition entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.models.embed import HarborEmbedClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.object_store import ARTIFACT_BUCKET, RAW_BUCKET
from harborrag_adapters.repositories.object_store.s3 import S3ObjectStore
from harborrag_adapters.repositories.vector import HarborVectorRepository
from harborrag_core.ingestion import ProcessingProfile
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_engine.ingestion import BaseChunker
from harborrag_runtime.config import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings

from .document.pipeline import DocumentStagePipeline
from .document.service import DocumentReleaseService
from .maintenance.cleanup import ProjectionCleanupService
from .maintenance.reindex import DocumentReindexService
from .maintenance.relation_repair import GraphRelationRepairService
from .observability import IngestionTelemetry
from .source.plan import SourcePlanRepository
from .source.service import SourceIngestionService

if TYPE_CHECKING:
    from harborrag_engine.ingestion import ChunkingConfig, ChunkStrategy

    from .document.normalizers import SourceDocumentNormalizerBuilder

logger = logging.getLogger("harborrag.runtime.ingestion.composition")


@dataclass(slots=True)
class IngestionRuntime:
    """Own connected providers and production ingestion application services."""

    connectors: Mapping[str, BaseConnector | HarborConnector]
    connector_fingerprints: Mapping[str, str]
    processing: ProcessingProfile
    control: IngestionControlPlaneDatabase
    documents: DocumentReleaseService
    stages: DocumentStagePipeline
    sources: SourceIngestionService
    relations: GraphRelationRepairService
    cleanup: ProjectionCleanupService
    reindex: DocumentReindexService
    source_plans: SourcePlanRepository
    object_store: S3ObjectStore
    vector_repository: HarborVectorRepository
    graph_repository: KnowledgeGraphRepositoryPort
    embed_client: HarborEmbedClient
    connector_rate_limiter: ConnectorRateLimiter
    telemetry: IngestionTelemetry
    _started: bool = field(default=False, init=False)

    async def start(self) -> None:
        if self._started:
            logger.debug("Ingestion runtime start skipped reason=already_started")
            return
        connected: list[Callable[[], Awaitable[None]]] = []
        connected_connectors: list[BaseConnector | HarborConnector] = []
        try:
            await self.telemetry.start()
            connected.append(self.telemetry.close)
            for resource in (
                self.control,
                self.object_store,
                self.vector_repository,
                self.graph_repository,
            ):
                await resource.connect()
                connected.append(resource.close)
            for connector in self.connectors.values():
                await asyncio.to_thread(connector.connect)
                connected_connectors.append(connector)
            await self.object_store.ensure_buckets((RAW_BUCKET, ARTIFACT_BUCKET))
        except BaseException as error:
            logger.error(
                "Ingestion runtime startup failed error_type=%s connected_resources=%d "
                "connected_connectors=%d",
                type(error).__name__,
                len(connected),
                len(connected_connectors),
            )
            await asyncio.gather(
                *(
                    asyncio.to_thread(connector.close)
                    for connector in reversed(connected_connectors)
                ),
                *(close() for close in reversed(connected)),
                asyncio.to_thread(self.connector_rate_limiter.close),
                self.embed_client.aclose(),
                return_exceptions=True,
            )
            raise
        self._started = True
        logger.info(
            "Ingestion runtime started resources=%d connectors=%d",
            len(connected),
            len(connected_connectors),
        )

    async def close(self) -> None:
        if not self._started:
            await asyncio.gather(
                asyncio.to_thread(self.connector_rate_limiter.close),
                self.embed_client.aclose(),
                self.telemetry.close(),
            )
            logger.debug("Ingestion runtime closed from unstarted state")
            return
        self._started = False
        try:
            await asyncio.gather(
                *(
                    asyncio.to_thread(connector.close)
                    for connector in reversed(tuple(self.connectors.values()))
                ),
                self.graph_repository.close(),
                self.vector_repository.close(),
                self.object_store.close(),
                self.control.close(),
                asyncio.to_thread(self.connector_rate_limiter.close),
                self.embed_client.aclose(),
                self.telemetry.close(),
            )
        except BaseException as error:
            logger.error(
                "Ingestion runtime shutdown failed error_type=%s",
                type(error).__name__,
            )
            raise
        logger.info("Ingestion runtime closed connectors=%d", len(self.connectors))

    def connector(
        self,
        name: str,
        *,
        configuration_fingerprint: str | None = None,
    ) -> BaseConnector | HarborConnector:
        try:
            connector = self.connectors[name]
        except KeyError as error:
            raise KeyError(f"ingestion connector is not configured: {name}") from error
        if (
            configuration_fingerprint is not None
            and self.connector_fingerprints.get(name) != configuration_fingerprint
        ):
            raise ConnectorConfigurationError(
                f"Connector {name!r} configuration differs between submission and worker"
            )
        return connector


def build_ingestion_runtime(
    settings: RuntimeSettings | None = None,
    *,
    normalizer_builder: SourceDocumentNormalizerBuilder | None = None,
    chunking_config: ChunkingConfig | None = None,
    chunking_strategies: tuple[ChunkStrategy, ...] = (),
) -> IngestionRuntime:
    """Build an ingestion runtime through the production object-graph builder."""

    from .runtime_builder import IngestionRuntimeBuilder

    return IngestionRuntimeBuilder(
        settings or RuntimeSettings(),
        normalizer_builder=normalizer_builder,
        chunking_config=chunking_config,
        chunking_strategies=chunking_strategies,
    ).build()


def _chunker() -> BaseChunker:
    """Compatibility hook for white-box chunking policy tests."""

    from .runtime_builder import build_default_chunker

    return build_default_chunker()
