"""Lazy resource factory: primary publication never depends on derived providers."""

from contextlib import AsyncExitStack
from dataclasses import dataclass

from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.models.runtime import ResourceOwnership
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.object_store import (
    ChunkArtifactReader,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_core.chunking import RecordKind
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.permissions import DerivedArtifactRecord
from harborrag_engine.topology.parent_builder import ParentDescriptionPolicy
from harborrag_runtime.composition.resources import build_vector_repository
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.observability import build_model_telemetry

from .contextual import ContextualMaterializer, ContextualResources
from .derived import DerivedEnrichmentCoordinator, DerivedResources, ParentProjectionPort
from .derived_cleanup import RetiredDerivedProjectionCleaner
from .derived_models import (
    DerivedBudget,
    DescriptionArtifacts,
    FrozenDescriptionGenerator,
    FrozenEmbedder,
    RequestEmbedder,
)
from .derived_projection import DerivedVectorProjection
from .description_factory import ConfiguredDescriptionGenerator
from .embedding_profile import build_contextual_profile
from .parent_materializer import ParentMaterializer
from .run_cost import RunCostLedger
from .service import extraction_input


@dataclass(frozen=True)
class DerivedRuntimeFactory:
    settings: RuntimeSettings
    control: IngestionControlPlaneDatabase
    graph: ParentProjectionPort
    reader: ImmutableArtifactReader
    writer: ImmutableArtifactWriter

    async def cleanup(
        self,
        build_id: str,
        records: tuple[DerivedArtifactRecord, ...],
        *,
        context: StorageOperationContext,
    ) -> int:
        """Open vector storage only for an explicit retired-projection cleanup."""

        if not records:
            return 0
        vectors = build_vector_repository(self.settings)
        try:
            await vectors.connect()
            return await RetiredDerivedProjectionCleaner(vectors).delete(
                build_id,
                records,
                context=context,
            )
        finally:
            await vectors.close()

    async def complete(self, tenant_id: str, build_id: str) -> dict[str, str]:
        if not self.settings.topology_derived_enabled:
            return {"contextual": "disabled", "parents": "disabled"}
        repository = self.control.topology
        build = await repository.get_build(tenant_id, build_id)
        if build is None or await repository.get_build_lineage(tenant_id, build_id) is None:
            raise ValueError("derived enrichment requires a current accepted build")
        job = await repository.get_job(tenant_id, build.job_id)
        if job is None:
            raise ValueError("accepted build has no canonical job")
        snapshot = await self.control.document_versions.get_version(job.document_version_id)
        if snapshot is None or snapshot.chunk_artifact is None:
            raise ValueError("accepted build has no frozen source chunks")
        chunks = await ChunkArtifactReader(self.reader).get_all(
            snapshot.chunk_artifact,
            context=StorageOperationContext.system(tenant_id),
            verify_integrity=True,
        )
        inputs = tuple(
            extraction_input(chunk, job=job)
            for chunk in chunks
            if chunk.record_kind == RecordKind.EVIDENCE and chunk.content.strip()
        )
        if tuple(value.chunk_id for value in inputs) != build.chunk_ids:
            raise ValueError("derived inputs do not match accepted graph coverage")
        async with AsyncExitStack() as stack:
            vectors = build_vector_repository(self.settings)
            stack.push_async_callback(vectors.close)
            await vectors.connect()
            embed_config = HarborEmbedClientConfig.from_file(self.settings.model_config_path)
            profile = build_contextual_profile(self.settings, embed_config)
            retry = embed_config.retry.model_copy(
                update={
                    "same_deployment_attempts": 1,
                    "max_deployment_failovers": 0,
                    "max_model_fallbacks": 0,
                }
            )
            bounded_config = embed_config.model_copy(update={"retry": retry})
            telemetry = build_model_telemetry(
                bounded_config, langfuse_enabled=self.settings.langfuse_enabled
            )
            try:
                embed = HarborEmbedClient.from_config(
                    bounded_config, telemetry=telemetry, telemetry_ownership=ResourceOwnership.OWNED
                )
            except BaseException:
                telemetry.close()
                raise
            stack.push_async_callback(embed.aclose)
            contextual_resources = ContextualResources(
                FrozenEmbedder(
                    RequestEmbedder(embed),
                    DescriptionArtifacts(self.reader, self.writer, profile.fingerprint),
                    tenant_id,
                    build_id,
                ),
                self.writer,
                self.reader,
            )
            descriptions = FrozenDescriptionGenerator(
                ConfiguredDescriptionGenerator(
                    self.settings,
                    job.policy.profile,
                    tenant_id,
                    job.document_id,
                ),
                DerivedBudget(
                    repository,
                    tenant_id,
                    build_id,
                    self.settings.topology_llm_operation_cost_usd,
                ),
                DescriptionArtifacts(
                    self.reader,
                    self.writer,
                    digest([job.policy.profile.fingerprint, profile.description_revision]),
                ),
                self.settings.topology_parent_max_output_tokens,
                RunCostLedger(ceiling_usd=self.settings.topology_parent_run_budget_usd),
            )
            coordinator = DerivedEnrichmentCoordinator(
                DerivedResources(
                    repository,
                    ContextualMaterializer(contextual_resources, profile),
                    ParentMaterializer(contextual_resources, profile),
                    descriptions,
                    DerivedVectorProjection(vectors, self.reader),
                    self.graph,
                    ParentDescriptionPolicy(
                        max_fan_in=self.settings.topology_parent_max_fan_in,
                        max_input_bytes=self.settings.topology_parent_max_input_bytes,
                        max_input_tokens=self.settings.topology_parent_max_input_tokens,
                        max_calls=self.settings.topology_parent_max_calls,
                    ),
                )
            )
            return await coordinator.complete(job, build, inputs)
