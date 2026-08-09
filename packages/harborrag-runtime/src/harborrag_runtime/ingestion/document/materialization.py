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

from .dependencies import DocumentReleaseDependencies
from .lifecycle import DocumentVersionLifecycle
from .materialization_helpers import representation_artifact_name
from .models import DocumentReleaseRequest
from .stage_models import PreparedDocumentStage


class DocumentMaterializationStages:
    """Persist canonical content, chunks, and dense/sparse representations."""

    def __init__(self, dependencies: DocumentReleaseDependencies) -> None:
        self._dependencies = dependencies
        self._lifecycle = DocumentVersionLifecycle(
            control=dependencies.control,
            canonical_artifacts=dependencies.canonical_artifacts,
            chunk_reader=dependencies.chunk_reader,
            projection_artifacts=dependencies.projection_artifacts,
        )

    async def sync_content_units(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        if not prepared.requires_processing:
            return
        document = await self._canonical(prepared, request=request)
        await self._dependencies.comment_artifacts.put(
            document,
            document_version_id=prepared.document_version_id,
            context=self._context(request),
        )

    async def persist_canonical(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        if not prepared.requires_processing:
            return
        document = await self._canonical(prepared, request=request)
        context = self._context(request)
        canonical_reference, _, _ = await asyncio.gather(
            self._dependencies.canonical_artifacts.put(
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                document=document,
                context=context,
            ),
            self._dependencies.comment_artifacts.put(
                document,
                document_version_id=prepared.document_version_id,
                context=context,
            ),
            self._dependencies.table_artifacts.put_all(
                document.table_artifacts,
                document_id=prepared.document_id,
                document_version_id=prepared.document_version_id,
                context=context,
            ),
        )
        await self._lifecycle.advance(
            prepared.document_version_id,
            DocumentVersionState.CANONICAL_READY,
            artifact_column="canonical_artifact",
            artifact=canonical_reference,
        )

    async def chunk_and_validate(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> ChunkingStatistics | None:
        if not prepared.requires_processing:
            return None
        snapshot = await self._required_snapshot(prepared.document_version_id)
        if snapshot.chunk_artifact is not None and snapshot.chunk_index_artifact is not None:
            return None
        document = await self._canonical(prepared, request=request)
        chunking = await asyncio.to_thread(
            self._dependencies.chunker.chunk,
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
