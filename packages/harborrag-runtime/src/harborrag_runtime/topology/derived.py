"""Post-accept coordinator: each derived product has an independent readiness gate."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from harborrag_core.ingestion import ArtifactReference
from harborrag_core.observability.log_events import log_failure
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
    """One derived artifact to record in the build's lineage.

    Attributes:
        artifact: The stored artifact.
        kind: Which derived product the artifact is.
        profile: Fingerprint of the profile that produced it.
        metadata: Product details recorded with the lineage row.
    """

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
    """Graph store side of parent descriptions."""

    async def write_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Write the build's parent descriptions to the graph."""
        ...

    async def verify_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> bool:
        """Return whether the graph holds the build's parent descriptions."""
        ...


@dataclass(frozen=True)
class DerivedResources:
    """Everything the derived products of one build read and write.

    Attributes:
        repository: Build lineage and derived-artifact publication records.
        contextual: Materializes contextual chunk representations.
        parents: Freezes and materializes parent descriptions.
        descriptions: Model that writes parent descriptions.
        vectors: Publishes derived vectors.
        graph: Writes and verifies parent descriptions in the graph.
        parent_policy: Limits for building parent descriptions.
        precomputed_parents: Parent descriptions already built, used instead of
            a model call.
        parent_loader: Loads parent descriptions instead of building them; it
            wins over ``precomputed_parents``.
    """

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
    """Build each derived product of an accepted build behind its own readiness gate.

    Attributes:
        resources: Stores, models and projections the products use.
    """

    resources: DerivedResources

    async def complete(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        inputs: tuple[ChunkExtractionInput, ...],
    ) -> dict[str, str]:
        """Build the contextual and parent products in turn and return their states.

        The products run one after the other but fail independently. A product
        whose build was superseded is skipped, a deferred one records the
        deferral, and any other failure is logged and leaves it pending.

        Args:
            job: The topology job that produced the build.
            build: The accepted build.
            inputs: The build's chunk extraction inputs.

        Returns:
            Each product's state: ``ready``, ``superseded``, ``not_applicable``,
            ``deferred:<reason>`` or ``pending:<error type>``.
        """
        states: dict[str, str] = {}
        if (
            build.projection_revision not in DERIVED_CAPABLE_PROJECTION_REVISIONS
            or not build.chunk_ids
        ):
            return {"contextual": "not_applicable", "parents": "not_applicable"}
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
            except Exception as error:  # noqa: BLE001 - the product stays pending and is retried
                log_failure(
                    logger,
                    "topology.derived_product_pending",
                    error,
                    tenant_id=job.tenant_id,
                    build_id=build.build_id,
                    product_kind=name,
                )
                states[name] = f"pending:{type(error).__name__}"
        return states

    async def _contextual(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        inputs: tuple[ChunkExtractionInput, ...],
    ) -> None:
        """Materialize and publish the build's contextual chunks."""
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
        """Build, project and publish the build's parent descriptions.

        Parents roll up the children's canonical content, read from immutable
        chunk artifacts. The vector payload is a lossy projection and is
        deliberately not the source.
        """
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
        """Publish a manifest's vectors, then record it in the build's lineage."""
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
        """Record one derived artifact against the build's current lineage."""
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
