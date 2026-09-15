"""Post-accept coordinator: each derived product has an independent readiness gate."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from harborrag_core.ingestion import ArtifactReference
from harborrag_core.ports.description_generation import DescriptionGeneratorPort
from harborrag_core.ports.topology import TopologyDerivationRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import ChunkExtractionInput, DocumentTopologyBuild, TopologyJob
from harborrag_core.topology.derived import ContextualManifest, ParentDescription, RollupSource
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.permissions import DerivedArtifactLineage
from harborrag_core.topology.revisions import DERIVED_CAPABLE_PROJECTION_REVISIONS
from harborrag_engine.topology.parent_builder import (
    ParentDescriptionBuilder,
    ParentDescriptionPolicy,
)

from .budgeted_extractor import EnrichmentDeferredError
from .contextual import ContextualMaterializer
from .derived_projection import DerivedVectorProjection
from .parent_materializer import ParentMaterializer

logger = logging.getLogger("harborrag.runtime.topology")


@dataclass(frozen=True)
class _DerivedPublication:
    artifact: ArtifactReference
    kind: Literal[
        "contextual_chunk",
        "parent_summary",
        "parent_graph_view",
        "parent_description",
    ]
    profile: str
    metadata: dict[str, JsonValue]


class ParentProjectionPort(Protocol):
    async def write_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> None: ...
    async def verify_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> bool: ...


@dataclass(frozen=True)
class DerivedResources:
    repository: TopologyDerivationRepositoryPort
    contextual: ContextualMaterializer
    parents: ParentMaterializer
    descriptions: DescriptionGeneratorPort
    vectors: DerivedVectorProjection
    graph: ParentProjectionPort
    parent_policy: ParentDescriptionPolicy = ParentDescriptionPolicy()
    precomputed_parents: tuple[ParentDescription, ...] | None = None
    parent_loader: Callable[[], Awaitable[tuple[ParentDescription, ...]]] | None = None


@dataclass(frozen=True)
class DerivedEnrichmentCoordinator:
    resources: DerivedResources

    async def complete(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        inputs: tuple[ChunkExtractionInput, ...],
    ) -> dict[str, str]:
        states: dict[str, str] = {}
        if (
            build.projection_revision not in DERIVED_CAPABLE_PROJECTION_REVISIONS
            or not build.chunk_ids
        ):
            return {"contextual": "not_applicable", "parents": "not_applicable"}
        # Sequential stages still have separate failure/readiness boundaries.
        for name, operation in (("contextual", self._contextual), ("parents", self._parents)):
            try:
                if (
                    await self.resources.repository.get_build_lineage(job.tenant_id, build.build_id)
                    is None
                ):
                    states[name] = "superseded"
                    continue
                await operation(job, build, inputs)
                states[name] = "ready"
            except EnrichmentDeferredError as error:
                states[name] = f"deferred:{error}"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Derived topology product remains pending tenant=%s build=%s product=%s",
                    job.tenant_id,
                    build.build_id,
                    name,
                )
                states[name] = f"pending:{type(error).__name__}"
        return states

    async def _contextual(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        inputs: tuple[ChunkExtractionInput, ...],
    ) -> None:
        manifest = await self.resources.contextual.materialize(
            job,
            build.build_id,
            inputs,
            {row.chunk_id: row for row in build.representations},
        )
        if manifest is None:
            raise ValueError("contextual representations are not complete")
        await self._publish(job, build, manifest, "contextual_chunk")

    async def _parents(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        inputs: tuple[ChunkExtractionInput, ...],
    ) -> None:
        # Roll up the children's canonical content, read from immutable chunk artifacts.
        # The vector payload is a lossy projection and is deliberately not the source.
        sources = tuple(
            RollupSource(
                chunk_id=value.chunk_id,
                text=value.content,
                section_path=value.heading_path,
                section_ids=value.section_ids,
            )
            for value in inputs
            if value.content.strip()
        )
        parents = self.resources.precomputed_parents
        if self.resources.parent_loader is not None:
            parents = await self.resources.parent_loader()
        if parents is None:
            parents = await ParentDescriptionBuilder(
                self.resources.descriptions, self.resources.parent_policy
            ).build(job.document_id, sources)
        context = StorageOperationContext.system(job.tenant_id)
        summary_artifact = await self.resources.parents.freeze_summaries(job, build, parents)
        summary_digest = digest([parent.model_dump(mode="json") for parent in parents])
        summary_profile = self.resources.parents.profile.parent_fingerprint
        await self._publish_artifact(
            job,
            build,
            _DerivedPublication(
                summary_artifact,
                "parent_summary",
                summary_profile,
                {
                    "summary_profile": summary_profile,
                    "summary_digest": summary_digest,
                    "parent_count": len(parents),
                    "semantic_coverage": "not_evaluated",
                },
            ),
        )
        await self.resources.graph.write_parents(build, parents, context=context)
        if not await self.resources.graph.verify_parents(build, parents, context=context):
            raise ValueError("parent graph verification failed")
        await self._publish_artifact(
            job,
            build,
            _DerivedPublication(
                summary_artifact,
                "parent_graph_view",
                summary_profile,
                {
                    "summary_profile": summary_profile,
                    "summary_digest": summary_digest,
                    "parent_count": len(parents),
                    "verified": True,
                },
            ),
        )
        manifest = await self.resources.parents.materialize(job, build, parents)
        await self._publish(job, build, manifest, "parent_description")

    async def _publish(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        manifest: ContextualManifest,
        kind: Literal["contextual_chunk", "parent_description"],
    ) -> None:
        context = StorageOperationContext.system(job.tenant_id)
        await self.resources.vectors.publish(build, manifest, context=context)
        await self._publish_artifact(
            job,
            build,
            _DerivedPublication(
                manifest.artifact,
                kind,
                manifest.embedding_profile,
                manifest.model_dump(mode="json", exclude={"artifact"}),
            ),
        )

    async def _publish_artifact(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        publication: _DerivedPublication,
    ) -> None:
        lineage = await self.resources.repository.get_build_lineage(job.tenant_id, build.build_id)
        if lineage is None:
            raise ValueError("build was superseded during derived projection")
        await self.resources.repository.publish_derivation(
            job.tenant_id,
            DerivedArtifactLineage(
                **lineage.model_dump(),
                artifact_id=digest([build.build_id, publication.kind, publication.profile]),
                artifact_kind=publication.kind,
                build_id=build.build_id,
                input_digest=digest([build.artifact.sha256, publication.profile]),
                metadata=publication.metadata,
            ),
            publication.artifact,
        )
