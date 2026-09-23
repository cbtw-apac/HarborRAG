"""Dependencies required by document release stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_core.ports.artifacts import (
    CanonicalCommentArtifactPort,
    CanonicalDocumentArtifactPort,
    CanonicalTableArtifactPort,
    ChunkArtifactReaderPort,
    ChunkArtifactWriterPort,
    ProjectionArtifactPort,
    RawDocumentArtifactPort,
)
from harborrag_core.ports.document_release import DocumentControlPort
from harborrag_engine.ingestion import (
    BaseChunker,
    BaseDocumentNormalizer,
    RepresentationReuseService,
    VectorProjectionStore,
)


class DocumentParserPort(Protocol):
    def parse(self, source: RawDocument, /) -> ParsedDocument: ...


@dataclass(frozen=True, slots=True)
class DocumentReleaseDependencies:
    """Explicit ports required to publish one document version."""

    parser: DocumentParserPort
    normalizer: BaseDocumentNormalizer
    chunker: BaseChunker
    representations: RepresentationReuseService
    control: DocumentControlPort
    raw_artifacts: RawDocumentArtifactPort
    canonical_artifacts: CanonicalDocumentArtifactPort
    comment_artifacts: CanonicalCommentArtifactPort
    table_artifacts: CanonicalTableArtifactPort
    chunk_writer: ChunkArtifactWriterPort
    chunk_reader: ChunkArtifactReaderPort
    projection_artifacts: ProjectionArtifactPort
    vector_store: VectorProjectionStore
    graph_store: KnowledgeGraphRepositoryPort
