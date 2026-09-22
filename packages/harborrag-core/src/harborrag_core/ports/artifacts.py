"""Provider-independent operations consumed by document release services."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harborrag_core.chunking import ChunkRecord, ConnectorType
from harborrag_core.domain import TableArtifact
from harborrag_core.domain.document import Document
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.ingestion import (
    ArtifactReference,
    CanonicalCommentSet,
    ChunkSetArtifacts,
    ContentReference,
    GraphEdgeRecord,
    GraphNodeRecord,
    RawDocumentReference,
    RepresentationSet,
    VectorEvidenceRecord,
)
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.storage import StorageOperationContext


class CanonicalDocumentArtifactPort(Protocol):
    async def put(
        self,
        *,
        document_id: str,
        document_version_id: str,
        document: Document,
        context: StorageOperationContext,
    ) -> ArtifactReference: ...

    async def get(
        self, reference: ArtifactReference, *, context: StorageOperationContext
    ) -> Document: ...


class ProjectionArtifactPort(Protocol):
    async def put_relations(
        self,
        *,
        document_id: str,
        document_version_id: str,
        relations: Sequence[GraphEdgeRecord],
        context: StorageOperationContext,
    ) -> ArtifactReference: ...

    async def put_vector_projection(
        self,
        *,
        document_id: str,
        document_version_id: str,
        points: Sequence[VectorEvidenceRecord],
        context: StorageOperationContext,
    ) -> ArtifactReference: ...

    async def put_graph_projection(
        self,
        *,
        document_id: str,
        document_version_id: str,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        context: StorageOperationContext,
    ) -> ArtifactReference: ...

    async def put_representation(
        self,
        *,
        document_id: str,
        document_version_id: str,
        encoder_profile: str,
        payload: bytes,
        context: StorageOperationContext,
    ) -> ArtifactReference: ...

    async def put_representation_set(
        self,
        representations: RepresentationSet,
        *,
        encoder_profile: str,
        context: StorageOperationContext,
    ) -> ArtifactReference: ...

    async def get_representation_set(
        self, reference: ArtifactReference, *, context: StorageOperationContext
    ) -> RepresentationSet: ...

    async def get_vector_projection(
        self, reference: ArtifactReference, *, context: StorageOperationContext
    ) -> tuple[VectorEvidenceRecord, ...]: ...

    async def get_graph_projection(
        self, reference: ArtifactReference, *, context: StorageOperationContext
    ) -> tuple[tuple[GraphNodeRecord, ...], tuple[GraphEdgeRecord, ...]]: ...


class ChunkArtifactWriterPort(Protocol):
    async def put(
        self,
        *,
        document_id: str,
        document_version_id: str,
        chunks: Sequence[ChunkRecord],
        context: StorageOperationContext,
    ) -> ChunkSetArtifacts: ...


class ChunkArtifactReaderPort(Protocol):
    async def get_chunk(
        self, artifacts: ChunkSetArtifacts, chunk_id: str, *, context: StorageOperationContext
    ) -> ChunkRecord: ...

    async def get_reference(
        self, reference: ContentReference, *, context: StorageOperationContext
    ) -> ChunkRecord: ...

    async def get_all(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
        verify_integrity: bool = False,
    ) -> tuple[ChunkRecord, ...]: ...

    async def get_artifacts(
        self,
        chunks: ArtifactReference,
        index: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> ChunkSetArtifacts: ...


class CanonicalCommentArtifactPort(Protocol):
    async def put(
        self, document: Document, *, document_version_id: str, context: StorageOperationContext
    ) -> tuple[CanonicalCommentSet, ArtifactReference]: ...

    async def get(
        self, reference: ArtifactReference, *, context: StorageOperationContext
    ) -> CanonicalCommentSet: ...


class RawDocumentArtifactPort(Protocol):
    async def put(
        self,
        *,
        connector: ConnectorType,
        document_id: DocumentId,
        document: RawDocument,
        context: StorageOperationContext,
    ) -> RawDocumentReference: ...

    async def get(
        self, reference: RawDocumentReference, *, context: StorageOperationContext
    ) -> RawDocument: ...


class CanonicalTableArtifactPort(Protocol):
    async def put_all(
        self,
        tables: tuple[TableArtifact, ...],
        *,
        document_id: str,
        document_version_id: str,
        context: StorageOperationContext,
    ) -> tuple[ArtifactReference, ...]: ...

    async def get_rows(
        self, reference: ArtifactReference, *, context: StorageOperationContext
    ) -> tuple[tuple[str | None, ...], ...]: ...
