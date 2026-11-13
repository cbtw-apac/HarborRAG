"""Canonical, chunk, and representation materialization stages."""

from __future__ import annotations

import asyncio

from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    DocumentVersionSnapshot,
    DocumentVersionState,
    SourceAdmissionDecision,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion.chunking import ChunkingRequest
from harborrag_engine.ingestion.chunking.schemas import ChunkingStatistics
from harborrag_engine.ingestion.release.materialization_helpers import representation_artifact_name
from harborrag_engine.ingestion.release.models import DocumentReleaseRequest
from harborrag_engine.ingestion.release.stage_models import PreparedDocumentStage

from .dependencies import DocumentReleaseDependencies
from .lifecycle import DocumentVersionLifecycle


class DocumentMaterializationStages:
    """Persist canonical content, chunks, and dense/sparse representations.

    Args:
        dependencies: Ports and stores the stages read and write.
    """

    def __init__(self, dependencies: DocumentReleaseDependencies) -> None:
        self._dependencies = dependencies
        self._lifecycle = DocumentVersionLifecycle(
            control=dependencies.control,
            canonical_artifacts=dependencies.canonical_artifacts,
            chunk_reader=dependencies.chunk_reader,
            projection_artifacts=dependencies.projection_artifacts,
            failures=dependencies.failures,
        )

    async def sync_content_units(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        """Do nothing; the stage is kept so in-flight workflow histories still replay.

        Its only output was the comment artifact, which ``build_projections`` writes and
        records in the manifest; nothing reads it before then.
        """
        del request, prepared

    async def persist_canonical(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        """Record the canonical artifact on the version.

        ``parse_and_normalize`` already wrote the canonical object, so this stage only
        records it: re-reading and re-writing the same immutable key cost a GET, two HEADs
        and a full serialization per document. Comment and table artifacts are written
        once, by ``build_projections``, which records them in the manifest.

        Raises:
            ValueError: No canonical artifact is available for the version.
        """
        if not prepared.requires_processing:
            return
        reference = prepared.canonical_reference
        if reference is None:
            snapshot = await self._required_snapshot(prepared.document_version_id)
            reference = snapshot.canonical_artifact
        if reference is None:
            raise ValueError("canonical document artifact is unavailable")
        await self._lifecycle.advance(
            prepared.document_version_id,
            DocumentVersionState.CANONICAL_READY,
            artifact_column="canonical_artifact",
            artifact=reference,
        )

    async def chunk_and_validate(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> ChunkingStatistics | None:
        """Chunk the canonical document and store the chunk artifacts.

        Returns:
            Chunking statistics, or ``None`` when nothing needs processing or the chunks
            already exist.
        """
        if not prepared.requires_processing:
            return None
        snapshot = await self._required_snapshot(prepared.document_version_id)
        if snapshot.chunk_artifact is not None and snapshot.chunk_index_artifact is not None:
            return None
        document = await self._canonical(prepared, request=request)
        chunker = (
            self._dependencies.chunker
            if self._dependencies.chunker_for is None
            else self._dependencies.chunker_for(request.run_settings)
        )
        chunking = await asyncio.to_thread(
            chunker.chunk,
            ChunkingRequest(
                tenant_id=request.tenant_id,
                document_version_id=prepared.document_version_id,
                document=document,
                connector_type=request.source_identity.connector_type.value,
                content_type=document.content_type,
            ),
        )
        artifacts = await self._dependencies.chunk_writer.put(
            document_id=prepared.document_id,
            document_version_id=prepared.document_version_id,
            chunks=chunking.chunks,
            context=self._context(request),
        )
        await self._lifecycle.record_chunks(
            prepared.document_version_id,
            artifacts,
        )
        return chunking.statistics

    async def encode_chunks(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        """Encode the version's chunks into dense and sparse representations.

        Reuses the active version's representations for a metadata-only change.

        Raises:
            ValueError: The version has no chunk artifact to encode.
        """
        if not prepared.requires_processing:
            return
        snapshot = await self._required_snapshot(prepared.document_version_id)
        if snapshot.representation_artifact is not None:
            return
        if snapshot.chunk_artifact is None:
            raise ValueError("canonical chunks are unavailable for encoding")
        context = self._context(request)
        chunks = await self._dependencies.chunk_reader.get_all(
            snapshot.chunk_artifact,
            context=context,
        )
        active = await self._dependencies.control.document_versions.active_snapshot(
            prepared.document_id
        )
        previous_chunks, previous_representations = await self._lifecycle.previous_representations(
            active,
            context=context,
            reuse=(prepared.decision == SourceAdmissionDecision.METADATA_CHANGED),
        )
        representations = await self._dependencies.representations.encode(
            chunks,
            previous_chunks=previous_chunks,
            previous_representations=previous_representations,
        )
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

    async def record_failure(
        self,
        prepared: PreparedDocumentStage,
        *,
        stage: str,
        error: Exception,
    ) -> None:
        """Record a stage failure on the version when it needs processing."""
        if not prepared.requires_processing:
            return
        await self._lifecycle.record_failure(
            document_id=prepared.document_id,
            document_version_id=prepared.document_version_id,
            stage=stage,
            error=error,
        )

    async def _canonical(
        self,
        prepared: PreparedDocumentStage,
        *,
        request: DocumentReleaseRequest,
    ) -> Document:
        reference = prepared.canonical_reference
        if reference is None:
            snapshot = await self._required_snapshot(prepared.document_version_id)
            reference = snapshot.canonical_artifact
        if reference is None:
            raise ValueError("canonical document artifact is unavailable")
        return await self._dependencies.canonical_artifacts.get(
            reference,
            context=self._context(request),
        )

    async def _required_snapshot(
        self,
        document_version_id: str,
    ) -> DocumentVersionSnapshot:
        snapshot = await self._dependencies.control.document_versions.get_version(
            document_version_id
        )
        if snapshot is None:
            raise ValueError("document version does not exist")
        return snapshot

    @staticmethod
    def _context(
        request: DocumentReleaseRequest,
    ) -> StorageOperationContext:
        return StorageOperationContext.system(request.tenant_id)
