"""Chunk, representation, and raw-boundary reuse for connector-free reindexing."""

from __future__ import annotations

from harborrag_core.ingestion import DocumentVersionSnapshot, DocumentVersionState
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import ChunkVersionRebinder
from harborrag_engine.ingestion.maintenance.reindex_plan import ReindexPlan
from harborrag_engine.ingestion.release.dependencies import DocumentReleaseDependencies
from harborrag_engine.ingestion.release.lifecycle import DocumentVersionLifecycle
from harborrag_engine.ingestion.release.materialization_helpers import representation_artifact_name
from harborrag_engine.ingestion.release.models import DocumentReleaseRequest
from harborrag_engine.ingestion.release.pipeline import DocumentStagePipeline
from harborrag_engine.ingestion.release.stage_models import PreparedDocumentStage


class ChunkReuseCoordinator:
    """Rebind existing chunks and representations onto a new document version.

    Args:
        dependencies: Ports and stores the reuse reads and writes.
        pipeline: Stage pipeline whose preparation stages the reuse runs through.
        lifecycle: Version state transitions and artifact records.
        chunk_rebinder: Rebinds previous chunks to the new document version.
    """

    def __init__(
        self,
        *,
        dependencies: DocumentReleaseDependencies,
        pipeline: DocumentStagePipeline,
        lifecycle: DocumentVersionLifecycle,
        chunk_rebinder: ChunkVersionRebinder,
    ) -> None:
        self._dependencies = dependencies
        self._pipeline = pipeline
        self._lifecycle = lifecycle
        self._chunk_rebinder = chunk_rebinder

    async def reuse_chunks_and_representations(
        self,
        *,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
        active: DocumentVersionSnapshot,
        plan: ReindexPlan,
    ) -> None:
        """Rebind the active version's chunks and reuse or regenerate its representations.

        Records a stage failure on the candidate before re-raising.

        Raises:
            ValueError: The active version has no reusable chunks or the configured
                encoders do not match the reindex target.
        """
        if not prepared.requires_processing:
            return
        try:
            if active.chunk_artifact is None or active.representation_artifact is None:
                raise ValueError("active version has no reusable chunk representations")
            await self._pipeline.preparation.persist_canonical(request, prepared)
            context = StorageOperationContext.system(request.tenant_id)
            previous_chunks = await self._dependencies.chunk_reader.get_all(
                active.chunk_artifact,
                context=context,
            )
            previous_representations = (
                await self._dependencies.projection_artifacts.get_representation_set(
                    active.representation_artifact,
                    context=context,
                )
            )
            chunks = self._chunk_rebinder.rebind(
                previous_chunks,
                document_version_id=prepared.document_version_id,
            )
            chunk_artifacts = await self._dependencies.chunk_writer.put(
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                chunks=chunks,
                context=context,
            )
            await self._lifecycle.record_chunks(
                prepared.document_version_id,
                chunk_artifacts,
            )
        except Exception as error:
            await self._pipeline.preparation.record_failure(
                prepared,
                stage="ChunkAndValidate",
                error=error,
            )
            raise
        try:
            representations = await self._dependencies.representations.reindex(
                chunks,
                previous_chunks=previous_chunks,
                previous_representations=previous_representations,
                regenerate_dense=plan.regenerate_dense,
                regenerate_sparse=plan.regenerate_sparse,
            )
            if representations.dense_profile_id != request.processing.dense_encoder_profile:
                raise ValueError("configured dense encoder does not match reindex target")
            if representations.sparse_profile_id != request.processing.sparse_encoder_profile:
                raise ValueError("configured sparse encoder does not match reindex target")
            reference = await self._dependencies.projection_artifacts.put_representation_set(
                representations,
                encoder_profile=representation_artifact_name(representations),
                context=context,
            )
            await self._lifecycle.advance(
                prepared.document_version_id,
                DocumentVersionState.REPRESENTATIONS_READY,
                artifact_column="representation_artifact",
                artifact=reference,
            )
        except Exception as error:
            await self._pipeline.preparation.record_failure(
                prepared,
                stage="EncodeChunks",
                error=error,
            )
            raise

    async def preserve_raw_boundary(
        self,
        prepared: PreparedDocumentStage,
        active: DocumentVersionSnapshot,
    ) -> None:
        """Carry immutable source provenance into a connector-free version."""
        if not prepared.requires_processing:
            return
        candidate = await self._dependencies.control.document_versions.get_version(
            prepared.document_version_id
        )
        if candidate is None:
            raise ValueError("reindex candidate disappeared before publication")
        for column, reference in (
            ("raw_artifact", active.raw_artifact),
            ("raw_metadata_artifact", active.raw_metadata_artifact),
        ):
            if reference is not None:
                await self._dependencies.control.document_versions.transition(
                    prepared.document_version_id,
                    candidate.state,
                    artifact_column=column,
                    artifact=reference,
                )
