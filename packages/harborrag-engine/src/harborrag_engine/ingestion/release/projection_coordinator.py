"""Coordinate rebuildable vector and graph projections."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    ArtifactReference,
    IndexVerificationResult,
    ProcessingProfile,
    ProjectionManifest,
)
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_core.ports.artifacts import ProjectionArtifactPort
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import (
    GraphProjectionBatch,
    GraphProjectionBuilder,
    ProjectionManifestBuilder,
    ProjectionManifestInput,
    ProjectionVerificationInput,
    ProjectionVerifier,
    VectorProjectionBatch,
    VectorProjectionStore,
)


@dataclass(frozen=True, slots=True)
class ProjectionVerificationRequest:
    """Everything the coordinator verifies against the projection stores.

    Args:
        manifest: Manifest the projections were built for.
        document: Canonical document.
        chunks: Chunk records of the version.
        vectors: Vector projection batch.
        graph: Graph projection batch.
        canonical_comment_ids: Comment IDs the canonical document carries.
    """

    manifest: ProjectionManifest
    document: Document
    chunks: tuple[ChunkRecord, ...]
    vectors: VectorProjectionBatch
    graph: GraphProjectionBatch
    canonical_comment_ids: tuple[str, ...] = ()


class DocumentProjectionCoordinator:
    """Coordinate pure projection building and projection-store verification.

    Args:
        projection_artifacts: Port that stores the projection artifacts.
        vector_store: Store the vector projection is verified against.
        graph_store: Store the graph projection is verified against.
    """

    def __init__(
        self,
        *,
        projection_artifacts: ProjectionArtifactPort,
        vector_store: VectorProjectionStore,
        graph_store: KnowledgeGraphRepositoryPort,
    ) -> None:
        self._projection_artifacts = projection_artifacts
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._graph_builder = GraphProjectionBuilder()
        self._manifest_builder = ProjectionManifestBuilder()
        self._verifier = ProjectionVerifier()

    def build_graph(
        self,
        *,
        processing: ProcessingProfile,
        document: Document,
        chunks: tuple[ChunkRecord, ...],
    ) -> GraphProjectionBatch:
        """Build the structural graph projection of a document version."""
        return self._graph_builder.build_structural(
            document=document,
            chunks=chunks,
            graph_projection_version=processing.graph_projection_version,
        )

    async def persist(
        self,
        *,
        document_id: str,
        document_version_id: str,
        vectors: VectorProjectionBatch,
        graph: GraphProjectionBatch,
        context: StorageOperationContext,
    ) -> tuple[ArtifactReference, ArtifactReference]:
        """Store the vector and graph projection artifacts concurrently.

        Returns:
            References to the vector artifact and the graph artifact, in that order.
        """
        return await asyncio.gather(
            self._projection_artifacts.put_vector_projection(
                document_id=document_id,
                document_version_id=document_version_id,
                points=vectors.evidence_records,
                context=context,
            ),
            self._projection_artifacts.put_graph_projection(
                document_id=document_id,
                document_version_id=document_version_id,
                nodes=graph.nodes,
                relations=graph.relations,
                context=context,
            ),
        )

    def manifest(
        self,
        request: ProjectionManifestInput,
    ) -> ProjectionManifest:
        """Build the projection manifest for a version."""
        return self._manifest_builder.build(request)

    async def verify(
        self,
        request: ProjectionVerificationRequest,
        *,
        context: StorageOperationContext,
    ) -> IndexVerificationResult:
        """Verify both projections against their stores concurrently and combine the results."""
        vector_result, graph_result = await asyncio.gather(
            self._vector_store.verify(request.vectors, context=context),
            self._graph_store.verify_projection(
                request.graph.nodes,
                request.graph.relations,
                context=context,
            ),
        )
        return self._verifier.verify(
            ProjectionVerificationInput(
                manifest=request.manifest,
                chunks=request.chunks,
                vectors=request.vectors,
                graph=request.graph,
                vector_result=vector_result,
                graph_result=graph_result,
                canonical_table_ids=tuple(
                    table.table_id for table in request.document.table_artifacts
                ),
                canonical_comment_ids=request.canonical_comment_ids,
            )
        )
