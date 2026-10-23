"""Independent resource lifecycle for optional topology workers and administration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from harborrag_adapters.models.chat import (
    ChatClientDependencies,
    ChatClientFactory,
    HarborChatClientConfig,
)
from harborrag_adapters.models.runtime import ResourceOwnership
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    ChunkArtifactReader,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_adapters.topology import LLMEntityExtractor, pinned_configuration
from harborrag_adapters.topology.artifacts import ExtractionArtifacts
from harborrag_core.ports.topology_extraction import UsageAwareExtractionPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import ExtractionProfile
from harborrag_core.topology.permissions import DerivedArtifactRecord
from harborrag_runtime.composition.resources import (
    build_ingestion_control,
    build_object_store,
    build_topology_repository,
)
from harborrag_runtime.composition.storage_providers import RuntimeTopologyPort
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.observability import build_model_telemetry

from .configuration import GraphBuildConfigSynchronizer, GraphBuildProfileFactory
from .derived_factory import DerivedRuntimeFactory
from .service import DerivationRunner, TopologyEnrichmentService, TopologyResources


class DerivationCleanupRunner(Protocol):
    async def __call__(
        self,
        build_id: str,
        records: tuple[DerivedArtifactRecord, ...],
        *,
        context: StorageOperationContext,
    ) -> int: ...


@asynccontextmanager
async def connect_topology_authority(
    settings: RuntimeSettings,
    *,
    migrate: bool = True,
) -> AsyncIterator[IngestionControlPlaneDatabase]:
    if migrate:
        from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations

        await asyncio.to_thread(run_migrations, settings.control_db_url.get_secret_value())
    control = build_ingestion_control(settings)
    try:
        await control.connect()
        yield control
    finally:
        await control.close()


@dataclass(frozen=True)
class TopologyRuntime:
    settings: RuntimeSettings
    control: IngestionControlPlaneDatabase
    service: TopologyEnrichmentService
    projection: RuntimeTopologyPort
    artifacts: ExtractionArtifacts
    artifact_reader: ImmutableArtifactReader
    artifact_writer: ImmutableArtifactWriter
    derive: DerivationRunner
    cleanup_derived: DerivationCleanupRunner


@asynccontextmanager
async def connect_topology_runtime(
    settings: RuntimeSettings, *, provision_graph: bool = True
) -> AsyncIterator[TopologyRuntime]:
    """No connector, embedder or vector collection is needed to enrich frozen chunks."""
    graph_build = GraphBuildConfig.from_settings(settings)
    settings = graph_build.effective_settings(settings)
    async with AsyncExitStack() as stack:
        control = await stack.enter_async_context(
            connect_topology_authority(settings, migrate=provision_graph)
        )
        if provision_graph:
            await GraphBuildConfigSynchronizer(
                graph_build,
                control.topology,
                GraphBuildProfileFactory(
                    settings.model_config_path, graph_build.resolved_ontologies
                ),
            ).apply()
        store = build_object_store(settings)
        stack.push_async_callback(store.close)
        await store.connect()
        if provision_graph:
            await store.ensure_buckets((ARTIFACT_BUCKET,))
        graph = build_topology_repository(settings)
        stack.push_async_callback(graph.close)
        await graph.connect(provision=provision_graph)
        reader = ImmutableArtifactReader(store)
        writer = ImmutableArtifactWriter(store)
        artifacts = ExtractionArtifacts(writer, reader)
        derived = DerivedRuntimeFactory(settings, control, graph, reader, writer)

        @asynccontextmanager
        async def extractor(profile: ExtractionProfile) -> AsyncIterator[UsageAwareExtractionPort]:
            config = pinned_configuration(
                HarborChatClientConfig.from_file(settings.model_config_path), profile
            )
            # Catalog parsing resolves model identity but performs no provider request.
            telemetry = build_model_telemetry(config, langfuse_enabled=settings.langfuse_enabled)
            try:
                client = ChatClientFactory.create_async(
                    config,
                    ChatClientDependencies(
                        telemetry=telemetry, telemetry_ownership=ResourceOwnership.OWNED
                    ),
                )
            except BaseException:
                telemetry.close()
                raise
            try:
                yield LLMEntityExtractor(
                    client, operation_seconds=settings.topology_operation_seconds
                )
            finally:
                await client.aclose()

        service = TopologyEnrichmentService(
            TopologyResources(
                control.topology,
                control.document_versions,
                ChunkArtifactReader(reader),
                artifacts,
                graph,
                extractor,
                llm_operation_cost_usd=settings.topology_llm_operation_cost_usd,
                require_budget=True,
                derive=derived.complete,
            ),
            lease_seconds=settings.topology_lease_seconds,
            job_seconds=settings.topology_job_seconds,
            max_chunks=settings.topology_max_chunks,
        )
        yield TopologyRuntime(
            settings,
            control,
            service,
            graph,
            artifacts,
            reader,
            writer,
            derived.complete,
            derived.cleanup,
        )
