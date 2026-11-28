"""Dependencies required by document release stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import IngestionFailureClassifier
from harborrag_core.ingestion.run_contracts import RunSettings
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
    """Parse one raw document into its structured form."""

    def parse(self, source: RawDocument, /) -> ParsedDocument:
        """Return the parsed form of ``source``."""
        ...


class RawDocumentLoader(Protocol):
    """Load the raw document behind one discovered source record."""

    def load(self, record: SourceRecord, /) -> RawDocument:
        """Return the raw document for ``record``."""
        ...


@dataclass(frozen=True, slots=True)
class DocumentReleaseDependencies:
    """Explicit ports required to publish one document version.

    Args:
        parser: Parser used when ``parser_for`` is not set.
        normalizer: Builds the canonical document from the parsed one.
        chunker: Chunker used when ``chunker_for`` is not set.
        representations: Reuses or encodes chunk representations.
        control: Control plane that stores version state.
        raw_artifacts: Raw document artifact store.
        canonical_artifacts: Canonical document artifact store.
        comment_artifacts: Canonical comment artifact store.
        table_artifacts: Canonical table artifact store.
        chunk_writer: Chunk artifact writer.
        chunk_reader: Chunk artifact reader.
        projection_artifacts: Relation and representation artifact store.
        vector_store: Vector projection store.
        graph_store: Knowledge graph repository.
        failures: Classifier that decides whether a stage error is retryable. Required
            so runtime supplies the infrastructure error types it treats as transient.
        parser_for: Picks the parser for a run's pinned settings.
        chunker_for: Picks the chunker for a run's pinned settings.
        representations_for: Picks the representation encoder for a run's pinned settings.
    """

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
    failures: IngestionFailureClassifier
    parser_for: Callable[[RunSettings | None], DocumentParserPort] | None = None
    chunker_for: Callable[[RunSettings | None], BaseChunker] | None = None
    representations_for: Callable[[RunSettings | None], RepresentationReuseService] | None = None
