"""Build, write, verify, and publish document projections."""

from __future__ import annotations

import asyncio

from harborrag_core.ingestion import (
    DocumentVersionState,
    ProjectionVerificationError,
)
from harborrag_engine.ingestion import (
    ProjectionManifestInput,
    PublicationPolicy,
    VectorProjectionBuilder,
    VectorProjectionInput,
)

from .dependencies import DocumentReleaseDependencies
from .lifecycle import DocumentVersionLifecycle
from .models import DocumentReleaseOutcome, DocumentReleaseRequest
from .projection_coordinator import (
    DocumentProjectionCoordinator,
    ProjectionVerificationRequest,
)
from .projection_material import ProjectionMaterialLoader, vector_batch
from .stage_models import PreparedDocumentStage


class DocumentProjectionStages:
    """Build, stage, verify, and publish rebuildable retrieval projections."""

    def __init__(self, dependencies: DocumentReleaseDependencies) -> None:
        self._dependencies = dependencies
        self._vectors = VectorProjectionBuilder()
        self._publication = PublicationPolicy()
        self._coordinator = DocumentProjectionCoordinator(
            projection_artifacts=dependencies.projection_artifacts,
            vector_store=dependencies.vector_store,
            graph_store=dependencies.graph_store,
        )
        self._lifecycle = DocumentVersionLifecycle(
            control=dependencies.control,
            canonical_artifacts=dependencies.canonical_artifacts,
            chunk_reader=dependencies.chunk_reader,
            projection_artifacts=dependencies.projection_artifacts,
        )
        self._material = ProjectionMaterialLoader(
            dependencies,
            self._coordinator,
        )

    async def build_relations(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        if not prepared.requires_processing:
            return
        material = await self._material.load(request, prepared)
        graph = self._material.graph(request, material)
        reference = await self._dependencies.projection_artifacts.put_relations(
            document_id=prepared.document_id,
            document_version_id=prepared.document_version_id,
            relations=graph.relations,
            context=self._material.context(request),
        )
        await self._lifecycle.advance(
            prepared.document_version_id,
            DocumentVersionState.REPRESENTATIONS_READY,
            artifact_column="relation_artifact",
            artifact=reference,
        )

    async def build_projections(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        if not prepared.requires_processing:
            return
        material = await self._material.load(request, prepared)
        graph = self._material.graph(request, material)
        vectors = self._vectors.build(
            VectorProjectionInput(
                chunks=material.chunks,
                representations=material.representations,
                chunk_artifacts=material.chunk_artifacts,
            )
        )
        context = self._material.context(request)
        (
            projection_references,
            comment_result,
            table_references,
        ) = await asyncio.gather(
            self._coordinator.persist(
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                vectors=vectors,
                graph=graph,
                context=context,
            ),
            self._dependencies.comment_artifacts.put(
                material.document,
                document_version_id=prepared.document_version_id,
                context=context,
            ),
            self._dependencies.table_artifacts.put_all(
                material.document.table_artifacts,
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                context=context,
            ),
        )
        vector_reference, graph_reference = projection_references
        comments, comment_reference = comment_result
        manifest = self._coordinator.manifest(
            ProjectionManifestInput(
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                chunks=material.chunks,
                vectors=vectors,
                graph=graph,
                canonical_table_ids=tuple(
                    sorted(table.table_id for table in material.document.table_artifacts)
                ),
                table_artifacts=table_references,
                canonical_comment_ids=tuple(comment.comment_id for comment in comments.comments),
                comment_artifact=comment_reference,
                vector_artifact=vector_reference,
                graph_artifact=graph_reference,
            )
        )
        await self._dependencies.control.document_versions.save_projection_manifest(manifest)

    async def write_vector_projection(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        if not prepared.requires_processing:
            return
        manifest = await self._material.manifest(prepared.document_version_id)
        if manifest.vector_artifact is None:
            raise ValueError("vector projection artifact is unavailable")
        points = await self._dependencies.projection_artifacts.get_vector_projection(
            manifest.vector_artifact,
            context=self._material.context(request),
        )
        await self._dependencies.vector_store.stage(
            vector_batch(points),
            context=self._material.context(request),
        )

    async def write_graph_projection(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        if not prepared.requires_processing:
            return
        manifest = await self._material.manifest(prepared.document_version_id)
        if manifest.graph_artifact is None:
            raise ValueError("graph projection artifact is unavailable")
        nodes, relations = await self._dependencies.projection_artifacts.get_graph_projection(
            manifest.graph_artifact,
            context=self._material.context(request),
        )
        await self._dependencies.graph_store.write_projection(
            nodes,
            relations,
            context=self._material.context(request),
        )
        await self._lifecycle.advance(
            prepared.document_version_id,
            DocumentVersionState.PROJECTIONS_STAGED,
        )

    async def verify_projections(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        if not prepared.requires_processing:
            return
        material = await self._material.load(request, prepared)
        manifest = await self._material.manifest(prepared.document_version_id)
        vectors, graph = await self._material.projection_batches(
            manifest,
            request=request,
        )
        comment_ids: tuple[str, ...] = ()
        if manifest.comment_artifact is not None:
            comments = await self._dependencies.comment_artifacts.get(
                manifest.comment_artifact,
                context=self._material.context(request),
            )
            comment_ids = tuple(comment.comment_id for comment in comments.comments)
        verification = await self._coordinator.verify(
            ProjectionVerificationRequest(
                manifest=manifest,
                document=material.document,
                chunks=material.chunks,
                vectors=vectors,
                graph=graph,
                canonical_comment_ids=comment_ids,
            ),
            context=self._material.context(request),
        )
        if not verification.valid:
            raise ProjectionVerificationError(
                "projection verification failed: " + "; ".join(verification.cross_projection_errors)
            )
        await self._dependencies.control.document_versions.mark_verified(
            prepared.document_version_id
        )

    async def publish_version(
        self,
        prepared: PreparedDocumentStage,
    ) -> DocumentReleaseOutcome:
        if not self._publication.requires_publication(prepared.decision):
            return DocumentReleaseOutcome(
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                decision=prepared.decision,
            )
        snapshot = await self._dependencies.control.document_versions.get_version(
            prepared.document_version_id
        )
        if snapshot is None:
            raise RuntimeError("publication candidate disappeared")
        self._publication.require_publishable(
            decision=prepared.decision,
            state=snapshot.state,
            requires_processing=prepared.requires_processing,
        )
        publication = await self._dependencies.control.publisher.publish(
            document_id=prepared.document_id,
            candidate_document_version_id=prepared.document_version_id,
        )
        if not prepared.requires_processing:
            return DocumentReleaseOutcome(
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                decision=prepared.decision,
                publication=publication,
            )
        manifest = await self._material.manifest(prepared.document_version_id)
        return DocumentReleaseOutcome(
            document_id=prepared.document_id,
            document_version_id=prepared.document_version_id,
            decision=prepared.decision,
            route_chunks=len(manifest.route_point_ids),
            evidence_chunks=len(manifest.evidence_point_ids),
            graph_nodes=len(manifest.graph_node_keys),
            graph_relations=len(manifest.graph_relation_ids),
            publication=publication,
        )
