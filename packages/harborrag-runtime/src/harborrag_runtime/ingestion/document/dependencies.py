"""Dependencies required by document release stages."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.parsers import HarborParserRegistry
from harborrag_adapters.repositories.database import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.object_store import (
    CanonicalCommentArtifactRepository,
    CanonicalDocumentArtifactRepository,
    CanonicalTableArtifactRepository,
    ChunkArtifactReader,
    ChunkArtifactWriter,
    ProjectionArtifactRepository,
    RawDocumentArtifactRepository,
)
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_engine.ingestion import (
    BaseChunker,
    BaseDocumentNormalizer,
    RepresentationReuseService,
    VectorProjectionStore,
)


@dataclass(frozen=True, slots=True)
class DocumentReleaseDependencies:
    """Explicit ports required to publish one document version."""

    parser: HarborParserRegistry
    normalizer: BaseDocumentNormalizer
    chunker: BaseChunker
    representations: RepresentationReuseService
    control: IngestionControlPlaneDatabase
    raw_artifacts: RawDocumentArtifactRepository
    canonical_artifacts: CanonicalDocumentArtifactRepository
    comment_artifacts: CanonicalCommentArtifactRepository
    table_artifacts: CanonicalTableArtifactRepository
    chunk_writer: ChunkArtifactWriter
    chunk_reader: ChunkArtifactReader
    projection_artifacts: ProjectionArtifactRepository
    vector_store: VectorProjectionStore
    graph_store: KnowledgeGraphRepositoryPort
