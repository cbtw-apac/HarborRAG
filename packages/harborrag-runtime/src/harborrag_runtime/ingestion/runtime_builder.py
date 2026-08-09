"""Builder for the production ingestion object graph."""

from __future__ import annotations

import os

from harborrag_adapters.chunking import RecursiveTextRefiner
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.connectors.registry import connector_registry
from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.models.runtime import ResourceOwnership
from harborrag_adapters.parsers import HarborParserRegistry
from harborrag_adapters.repositories.object_store import (
    CanonicalCommentArtifactRepository,
    CanonicalDocumentArtifactRepository,
    CanonicalTableArtifactRepository,
    ChunkArtifactReader,
    ChunkArtifactWriter,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    ProjectionArtifactRepository,
    RawDocumentArtifactRepository,
)
from harborrag_core.ingestion import SparseEncoderProfile
from harborrag_engine.ingestion import (
    BaseChunker,
    BaseDocumentNormalizer,
    BM25SparseEncoder,
    ChunkingConfig,
    ChunkRepresentationEncoder,
    ChunkStrategy,
    RepresentationEncodingPolicy,
    RepresentationReuseService,
    VectorProjectionPolicy,
    VectorProjectionStore,
    build_chunking_service,
)
from harborrag_runtime.config import (
    connector_fingerprint,
    load_connector_catalog,
    load_parser_catalog,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion_control_factory import build_ingestion_control
from harborrag_runtime.model_configuration import embedding_dimensions
from harborrag_runtime.rate_limiting import build_connector_rate_limiter
from harborrag_runtime.storage_factory import (
    build_knowledge_graph,
    build_object_store,
    build_vector_repository,
)
from harborrag_runtime.tokenization import ApproximateTokenCounter

from .composition import IngestionRuntime
from .document.dependencies import DocumentReleaseDependencies
from .document.normalizers import (
    SourceDocumentNormalizerBuilder,
    default_source_document_normalizer_builder,
)
from .document.pipeline import DocumentStagePipeline
from .document.service import DocumentReleaseService
from .maintenance.cleanup import ProjectionCleanupService
from .maintenance.reindex import DocumentReindexService
from .maintenance.relation_repair import GraphRelationRepairService
from .observability import IngestionTelemetry, build_model_telemetry
from .profiles import build_processing_profile
from .source.plan import SourcePlanRepository
from .source.service import SourceIngestionService


class IngestionRuntimeBuilder:
    """Assemble one runtime graph without performing external I/O."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        normalizer_builder: SourceDocumentNormalizerBuilder | None = None,
        chunking_config: ChunkingConfig | None = None,
        chunking_strategies: tuple[ChunkStrategy, ...] = (),
    ) -> None:
        self._settings = settings
        self._normalizer_builder = (
            normalizer_builder or default_source_document_normalizer_builder()
        )
        self._chunking_config = chunking_config
        self._chunking_strategies = tuple(chunking_strategies)

    def build(self) -> IngestionRuntime:
        settings = self._settings
        telemetry = IngestionTelemetry(
            metrics_port=settings.metrics_port,
            metrics_bind_address=settings.metrics_bind_address,
        )
        parser = load_parser_catalog(settings.parser_config_path).build_harbor_parser()
        rate_limiter = self._rate_limiter(telemetry)
        connectors, connector_fingerprints = self._connectors(
            attachment_parser=parser,
            rate_limiter=rate_limiter,
        )
        embed_client, model, dimensions = self._embedding_client()
        control = build_ingestion_control(settings)
        object_store = build_object_store(settings)
        vectors = build_vector_repository(settings)
        graph = build_knowledge_graph(settings)
        artifacts = ImmutableArtifactWriter(object_store)
        artifact_reader = ImmutableArtifactReader(object_store)
        vector_store = VectorProjectionStore(
            vectors,
            VectorProjectionPolicy(dimension=dimensions),
        )
        canonical_artifacts = CanonicalDocumentArtifactRepository(
            artifacts,
            artifact_reader,
        )
        chunk_reader = ChunkArtifactReader(artifact_reader)
        dependencies = DocumentReleaseDependencies(
            parser=parser,
            normalizer=self.build_document_normalizer(),
            chunker=build_default_chunker(
                config=self._chunking_config,
                additional_strategies=self._chunking_strategies,
            ),
            representations=RepresentationReuseService(
                ChunkRepresentationEncoder(
                    embed_client,
                    BM25SparseEncoder(self._sparse_profile()),
                    RepresentationEncodingPolicy(
                        logical_model=model,
                        dense_profile_id=settings.dense_encoder_profile,
                        dense_dimension=dimensions,
                    ),
                )
            ),
            control=control,
            raw_artifacts=RawDocumentArtifactRepository(artifacts, artifact_reader),
            canonical_artifacts=canonical_artifacts,
            comment_artifacts=CanonicalCommentArtifactRepository(
                artifacts,
                artifact_reader,
            ),
            table_artifacts=CanonicalTableArtifactRepository(
                artifacts,
                artifact_reader,
            ),
            chunk_writer=ChunkArtifactWriter(artifacts),
            chunk_reader=chunk_reader,
            projection_artifacts=ProjectionArtifactRepository(
                artifacts,
                artifact_reader,
            ),
            vector_store=vector_store,
            graph_store=graph,
        )

        stages = DocumentStagePipeline(dependencies)
        documents = DocumentReleaseService(dependencies, pipeline=stages)
        relations = GraphRelationRepairService(
            control=control,
            canonical_artifacts=canonical_artifacts,
            chunk_reader=chunk_reader,
            graph_store=graph,
            max_concurrency=settings.graph_relation_repair_concurrency,
        )
        return IngestionRuntime(
            connectors=connectors,
            connector_fingerprints=connector_fingerprints,
            processing=build_processing_profile(settings),
            control=control,
            documents=documents,
            stages=stages,
            sources=SourceIngestionService(
                control=control,
                documents=documents,
                relations=relations,
            ),
            relations=relations,
            cleanup=ProjectionCleanupService(
                control=control,
                vector_store=vector_store,
                graph_store=graph,
            ),
            reindex=DocumentReindexService(dependencies, pipeline=stages),
            source_plans=SourcePlanRepository(artifacts, artifact_reader),
            object_store=object_store,
            vector_repository=vectors,
            graph_repository=graph,
            embed_client=embed_client,
            connector_rate_limiter=rate_limiter,
            telemetry=telemetry,
        )

    def build_document_normalizer(self) -> BaseDocumentNormalizer:
        """Build the configured immutable provider router."""

        return self._normalizer_builder.build()

    def _rate_limiter(self, telemetry: IngestionTelemetry) -> ConnectorRateLimiter:
        return build_connector_rate_limiter(
            self._settings,
            on_wait=lambda scope, wait_seconds: telemetry.record_rate_limit_wait(
                scope.connector_type,
                wait_seconds,
            ),
        )

    def _connectors(
        self,
        *,
        attachment_parser: HarborParserRegistry,
        rate_limiter: ConnectorRateLimiter,
    ) -> tuple[dict[str, BaseConnector | HarborConnector], dict[str, str]]:
        catalog = load_connector_catalog(self._settings.connector_config_path)
        names = catalog.names(enabled_only=True)
        dependencies = {
            "attachment_parser": attachment_parser,
            "rate_limiter": rate_limiter,
        }
        connectors: dict[str, BaseConnector | HarborConnector] = {
            name: catalog.build(
                name,
                connector_kwargs=self._connector_kwargs(
                    catalog.get(name).provider,
                    dependencies,
                ),
            )
            for name in names
        }
        fingerprints = {
            name: connector_fingerprint(
                catalog_version=catalog.version,
                definition=catalog.get(name),
                environment=os.environ,
            )
            for name in names
        }
        return connectors, fingerprints

    @staticmethod
    def _connector_kwargs(
        provider: str,
        dependencies: dict[str, object],
    ) -> dict[str, object]:
        definition = connector_registry.get_definition(provider)
        missing = set(definition.constructor_dependencies.values()).difference(dependencies)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(
                f"connector provider {provider!r} requires unavailable runtime "
                f"dependencies: {names}"
            )
        return {
            argument: dependencies[dependency]
            for argument, dependency in definition.constructor_dependencies.items()
        }

    def _embedding_client(self) -> tuple[HarborEmbedClient, str, int]:
        settings = self._settings
        config = HarborEmbedClientConfig.from_file(settings.model_config_path)
        model = settings.embedding_model or config.default_model
        dimensions = settings.embedding_dimensions or embedding_dimensions(config, model)
        client = HarborEmbedClient.from_config(
            config,
            telemetry=build_model_telemetry(
                config,
                langfuse_enabled=settings.langfuse_enabled,
            ),
            telemetry_ownership=ResourceOwnership.OWNED,
        )
        return client, model, dimensions

    def _sparse_profile(self) -> SparseEncoderProfile:
        settings = self._settings
        return SparseEncoderProfile(
            profile_id=settings.sparse_encoder_profile,
            k=settings.sparse_k,
            b=settings.sparse_b,
            fixed_avg_len=settings.sparse_fixed_avg_len,
        )


def build_default_chunker(
    *,
    config: ChunkingConfig | None = None,
    additional_strategies: tuple[ChunkStrategy, ...] = (),
) -> BaseChunker:
    """Build canonical chunking with optional source-owned extensions."""

    token_counter = ApproximateTokenCounter()
    return build_chunking_service(
        config=(
            config
            or ChunkingConfig(
                configuration_version="canonical-source-policies",
                create_route_chunks=True,
            )
        ),
        token_counter=token_counter,
        refiner=RecursiveTextRefiner(token_counter),
        additional_strategies=additional_strategies,
    )
