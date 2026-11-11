"""Load and map material required by projection stages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    ChunkSetArtifacts,
    ProjectionManifest,
    RepresentationSet,
    VectorEvidenceRecord,
    VectorProjectionBatch,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import (
    GraphProjectionBatch,
)
from harborrag_engine.ingestion.release.projection_coordinator import DocumentProjectionCoordinator
from harborrag_engine.ingestion.release.stage_models import PreparedDocumentStage

from .dependencies import DocumentReleaseDependencies
from .models import DocumentReleaseRequest


@dataclass(frozen=True, slots=True)
class ProjectionMaterial:
    """Inputs the projection stages rebuild from.

    Args:
        document: Canonical document.
        chunks: Chunk records of the version.
        chunk_artifacts: References to the stored chunk set.
        representations: Dense and sparse representations of the chunks.
    """

    document: Document
    chunks: tuple[ChunkRecord, ...]
    chunk_artifacts: ChunkSetArtifacts
    representations: RepresentationSet


class ProjectionMaterialLoader:
    """Reload large projection inputs inside activities, never workflow history.

    Args:
        dependencies: Ports and stores the material is read from.
        coordinator: Builds the graph projection from the loaded material.
    """

    def __init__(
        self,
        dependencies: DocumentReleaseDependencies,
        coordinator: DocumentProjectionCoordinator,
    ) -> None:
        self._dependencies = dependencies
        self._coordinator = coordinator

    async def load(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> ProjectionMaterial:
        """Read the version's canonical document, chunks and representations.

        Raises:
            ValueError: The version does not exist or its projection inputs are incomplete.
        """
        snapshot = await self._dependencies.control.document_versions.get_version(
            prepared.document_version_id
        )
        if snapshot is None:
            raise ValueError("document version does not exist")
        required = (
            snapshot.canonical_artifact,
            snapshot.chunk_artifact,
            snapshot.chunk_index_artifact,
            snapshot.representation_artifact,
        )
        if any(reference is None for reference in required):
            raise ValueError("projection inputs are incomplete")
        if snapshot.canonical_artifact is None:
            raise HarborInvariantError("snapshot.canonical_artifact must not be None here")
        if snapshot.chunk_artifact is None:
            raise HarborInvariantError("snapshot.chunk_artifact must not be None here")
        if snapshot.chunk_index_artifact is None:
            raise HarborInvariantError("snapshot.chunk_index_artifact must not be None here")
        if snapshot.representation_artifact is None:
            raise HarborInvariantError("snapshot.representation_artifact must not be None here")
        context = self.context(request)
        canonical, chunks, chunk_artifacts, representations = await asyncio.gather(
            self._dependencies.canonical_artifacts.get(
                snapshot.canonical_artifact,
                context=context,
            ),
            self._dependencies.chunk_reader.get_all(
                snapshot.chunk_artifact,
                context=context,
            ),
            self._dependencies.chunk_reader.get_artifacts(
                snapshot.chunk_artifact,
                snapshot.chunk_index_artifact,
                context=context,
            ),
            self._dependencies.projection_artifacts.get_representation_set(
                snapshot.representation_artifact,
                context=context,
            ),
        )
        return ProjectionMaterial(
            document=canonical,
            chunks=chunks,
            chunk_artifacts=chunk_artifacts,
            representations=representations,
        )

    def graph(
        self,
        request: DocumentReleaseRequest,
        material: ProjectionMaterial,
    ) -> GraphProjectionBatch:
        """Build the structural graph projection of the loaded material."""
        return self._coordinator.build_graph(
            processing=request.processing,
            document=material.document,
            chunks=material.chunks,
        )

    async def projection_batches(
        self,
        manifest: ProjectionManifest,
        *,
        request: DocumentReleaseRequest,
    ) -> tuple[VectorProjectionBatch, GraphProjectionBatch]:
        """Read the manifest's stored vector and graph projections.

        Raises:
            ValueError: The manifest lacks a vector or graph artifact.
        """
        if manifest.vector_artifact is None or manifest.graph_artifact is None:
            raise ValueError("projection artifacts are unavailable")
        points, graph_records = await asyncio.gather(
            self._dependencies.projection_artifacts.get_vector_projection(
                manifest.vector_artifact,
                context=self.context(request),
            ),
            self._dependencies.projection_artifacts.get_graph_projection(
                manifest.graph_artifact,
                context=self.context(request),
            ),
        )
        nodes, relations = graph_records
        return vector_batch(points), GraphProjectionBatch(
            nodes=nodes,
            relations=relations,
        )

    async def manifest(
        self,
        document_version_id: str,
    ) -> ProjectionManifest:
        """Return the projection manifest of a version.

        Raises:
            ValueError: No manifest exists.
        """
        manifest = await self._dependencies.control.reliability.projection_manifest(
            document_version_id
        )
        if manifest is None:
            raise ValueError("projection manifest is unavailable")
        return manifest

    @staticmethod
    def context(
        request: DocumentReleaseRequest,
    ) -> StorageOperationContext:
        """Return the system storage context of the request's tenant."""
        return StorageOperationContext.system(request.tenant_id)


def vector_batch(
    points: tuple[VectorEvidenceRecord, ...],
) -> VectorProjectionBatch:
    """Assemble stored evidence records into a vector projection batch."""
    return VectorProjectionBatch.assemble(evidence_records=tuple(points))
