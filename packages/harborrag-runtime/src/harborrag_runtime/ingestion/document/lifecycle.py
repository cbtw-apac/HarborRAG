"""Durable document-version lifecycle transitions."""

from __future__ import annotations

import logging

from harborrag_adapters.repositories.database import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store import (
    CanonicalDocumentArtifactRepository,
    ChunkArtifactReader,
    ProjectionArtifactRepository,
)
from harborrag_core.chunking import ChunkRecord
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    ArtifactReference,
    ChunkSetArtifacts,
    DocumentFailure,
    DocumentVersionSnapshot,
    DocumentVersionState,
    RawDocumentReference,
    RepresentationSet,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import (
    DocumentVersionTransitionPolicy,
    IngestionFailureClassifier,
)

logger = logging.getLogger("harborrag.runtime.ingestion.version_lifecycle")


class DocumentVersionLifecycle:
    """Own durable version transitions, replay boundaries, and compensation."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        canonical_artifacts: CanonicalDocumentArtifactRepository,
        chunk_reader: ChunkArtifactReader,
        projection_artifacts: ProjectionArtifactRepository,
    ) -> None:
        self._control = control
        self._canonical_artifacts = canonical_artifacts
        self._chunk_reader = chunk_reader
        self._projection_artifacts = projection_artifacts
        self._failures = IngestionFailureClassifier()
        self._transitions = DocumentVersionTransitionPolicy()

    async def prepare_replay(
        self,
        document_version_id: str,
        current: DocumentVersionState,
    ) -> DocumentVersionState:
        if current not in {
            DocumentVersionState.FAILED,
            DocumentVersionState.RETIRED,
        }:
            return current
        return await self._control.document_versions.prepare_replay(document_version_id)

    async def materialize_document(
        self,
        *,
        document_version_id: str,
        current: DocumentVersionState,
        candidate: Document,
        context: StorageOperationContext,
    ) -> tuple[Document, DocumentVersionSnapshot]:
        """Prefer an existing immutable canonical boundary during replay."""

        await self.prepare_replay(document_version_id, current)
        snapshot = await self._required_snapshot(document_version_id)
        if snapshot.canonical_artifact is None:
            return candidate, snapshot
        document = await self._canonical_artifacts.get(
            snapshot.canonical_artifact,
            context=context,
        )
        return document, snapshot

    async def record_raw(
        self,
        document_version_id: str,
        reference: RawDocumentReference,
    ) -> None:
        await self.advance(
            document_version_id,
            DocumentVersionState.RAW_CAPTURED,
            artifact_column="raw_artifact",
            artifact=reference.source_artifact,
        )
        await self.advance(
            document_version_id,
            DocumentVersionState.RAW_CAPTURED,
            artifact_column="raw_metadata_artifact",
            artifact=reference.metadata_artifact,
        )

    async def record_chunks(
        self,
        document_version_id: str,
        artifacts: ChunkSetArtifacts,
    ) -> None:
        await self.advance(
            document_version_id,
            DocumentVersionState.CHUNKS_READY,
            artifact_column="chunk_artifact",
            artifact=artifacts.chunks,
        )
        await self.advance(
            document_version_id,
            DocumentVersionState.CHUNKS_READY,
            artifact_column="chunk_index_artifact",
            artifact=artifacts.index,
        )

    async def advance(
        self,
        document_version_id: str,
        state: DocumentVersionState,
        *,
        artifact_column: str | None = None,
        artifact: ArtifactReference | None = None,
    ) -> None:
        snapshot = await self._required_snapshot(document_version_id)
        if snapshot.state == DocumentVersionState.FAILED:
            await self._control.document_versions.prepare_replay(document_version_id)
            snapshot = await self._required_snapshot(document_version_id)
        if self._transitions.already_reached(snapshot.state, state):
            return
        self._transitions.require(snapshot.state, state)
        await self._control.document_versions.transition(
            document_version_id,
            state,
            artifact_column=artifact_column,
            artifact=artifact,
        )

    async def previous_representations(
        self,
        active: DocumentVersionSnapshot | None,
        *,
        context: StorageOperationContext,
        reuse: bool,
    ) -> tuple[tuple[ChunkRecord, ...], RepresentationSet | None]:
        if (
            not reuse
            or active is None
            or active.chunk_artifact is None
            or active.representation_artifact is None
        ):
            return (), None
        try:
            chunks = await self._chunk_reader.get_all(
                active.chunk_artifact,
                context=context,
            )
            representations = await self._projection_artifacts.get_representation_set(
                active.representation_artifact,
                context=context,
            )
        except HarborStorageNotFoundError:
            # Reuse is an optimization, never a correctness requirement. An
            # operator may have cleaned an old object-store generation while
            # Postgres still points at the active version. Encode the new
            # candidate from its own chunks instead of failing the ingestion.
            logger.warning(
                "Previous representation artifacts are unavailable; "
                "encoding without reuse document_version_id=%s",
                active.document_version_id,
            )
            return (), None
        return chunks, representations

    async def record_failure(
        self,
        *,
        document_id: str,
        document_version_id: str,
        stage: str,
        error: Exception,
    ) -> None:
        snapshot = await self._control.document_versions.get_version(document_version_id)
        if snapshot is None or snapshot.state == DocumentVersionState.ACTIVE:
            return
        failure = self._failures.classify(stage, error)
        artifact_references = await self._artifact_references(snapshot)
        try:
            await self._control.document_versions.transition(
                document_version_id,
                DocumentVersionState.FAILED,
            )
        finally:
            await self._control.reliability.record_failure(
                DocumentFailure(
                    document_id=DocumentId(document_id),
                    document_version_id=DocumentVersionId(document_version_id),
                    failed_stage=stage,
                    category=failure.category,
                    retryable=failure.retryable,
                    safe_error_code=failure.code,
                    artifact_references=artifact_references,
                )
            )
            await self._control.reliability.enqueue_cleanup(
                document_id=document_id,
                document_version_id=document_version_id,
            )

    async def _required_snapshot(
        self,
        document_version_id: str,
    ) -> DocumentVersionSnapshot:
        snapshot = await self._control.document_versions.get_version(document_version_id)
        if snapshot is None:
            raise RuntimeError("document version disappeared during ingestion")
        return snapshot

    async def _artifact_references(
        self,
        snapshot: DocumentVersionSnapshot,
    ) -> tuple[ArtifactReference, ...]:
        references = [
            reference
            for reference in (
                snapshot.raw_artifact,
                snapshot.raw_metadata_artifact,
                snapshot.canonical_artifact,
                snapshot.chunk_artifact,
                snapshot.chunk_index_artifact,
                snapshot.relation_artifact,
                snapshot.representation_artifact,
            )
            if reference is not None
        ]
        manifest = await self._control.reliability.projection_manifest(
            str(snapshot.document_version_id)
        )
        if manifest is not None:
            references.extend(
                reference
                for reference in (
                    *manifest.table_artifacts,
                    manifest.comment_artifact,
                    manifest.vector_artifact,
                    manifest.graph_artifact,
                )
                if reference is not None
            )
        unique = {
            (
                reference.bucket,
                reference.key,
                reference.sha256,
                reference.byte_offset,
                reference.byte_length,
            ): reference
            for reference in references
        }
        return tuple(unique.values())
