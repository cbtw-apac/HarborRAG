"""Document release object-graph fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.sqlite.client import SQLiteDBClient
from harborrag_adapters.repositories.object_store import (
    CanonicalCommentArtifactRepository,
    CanonicalDocumentArtifactRepository,
    CanonicalTableArtifactRepository,
    ChunkArtifactReader,
    ChunkArtifactWriter,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
    ProjectionArtifactRepository,
    RawDocumentArtifactRepository,
)
from harborrag_core.chunking import ConnectorType
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ProcessingProfile,
    SourceAdmissionDecision,
    SourceIdentity,
    SparseEncoderProfile,
)
from harborrag_engine.ingestion import (
    BM25SparseEncoder,
    ChunkingConfig,
    ChunkRepresentationEncoder,
    DocumentNormalizer,
    RepresentationEncodingPolicy,
    RepresentationReuseService,
    VectorProjectionPolicy,
    VectorProjectionStore,
    build_chunking_service,
)
from harborrag_runtime.ingestion import (
    DocumentReleaseDependencies,
    DocumentReleaseRequest,
    DocumentReleaseService,
    GraphRelationRepairService,
    SourceIngestionRequest,
)
from harborrag_runtime.tokenization import ApproximateTokenCounter

from .connectors import (
    DeterministicEmbedClient,
    TextParser,
)
from .storage import (
    InMemoryKnowledgeGraph,
    InMemoryVectorRepository,
)


def build_control_plane(tmp_path: Path) -> IngestionControlPlaneDatabase:
    return IngestionControlPlaneDatabase(
        SQLiteDBClient(database=str(tmp_path / "release.db")),
        create_schema=True,
    )


def processing_profile() -> ProcessingProfile:
    return ProcessingProfile(
        parser_profile="text-v1",
        normalizer_version="canonical-v1",
        chunk_strategy="route-evidence-v3",
        dense_encoder_profile="dense-v1",
        sparse_encoder_profile="bm25-v1",
        graph_projection_version="graph-v1",
    )


def release_request(
    *,
    source_version: str,
    discovery_decision: SourceAdmissionDecision | None = None,
) -> DocumentReleaseRequest:
    return DocumentReleaseRequest(
        tenant_id="default",
        connector_name="local-docs",
        source=SourceRecord(
            id="docs/worker.txt",
            source_type="text/plain",
            locator="file:///docs/worker.txt",
        ),
        source_identity=SourceIdentity(
            tenant_id="default",
            connector_type=ConnectorType.LOCAL,
            connection_id="local-docs",
            source_item_id="docs/worker.txt",
            source_scope_id="docs",
        ),
        admission=AdmissionSnapshot(source_version=source_version),
        processing=processing_profile(),
        discovery_decision=discovery_decision,
    )


@dataclass(slots=True)
class ReleaseResources:
    control: IngestionControlPlaneDatabase
    store: MemoryObjectStore
    parser: TextParser
    embed: DeterministicEmbedClient
    vectors: InMemoryVectorRepository
    graph: InMemoryKnowledgeGraph


def build_dependencies(
    resources: ReleaseResources,
) -> DocumentReleaseDependencies:
    writer = ImmutableArtifactWriter(resources.store)
    reader = ImmutableArtifactReader(resources.store)
    token_counter = ApproximateTokenCounter()
    chunker = build_chunking_service(
        config=ChunkingConfig(
            configuration_version="3",
            create_route_chunks=True,
        ),
        token_counter=token_counter,
        refiner=SimpleTextRefiner(token_counter),
    )
    representation_encoder = ChunkRepresentationEncoder(
        resources.embed,
        BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1")),
        RepresentationEncodingPolicy(
            logical_model="dense-model",
            dense_profile_id="dense-v1",
            dense_dimension=3,
        ),
    )
    return DocumentReleaseDependencies(
        parser=resources.parser,
        normalizer=DocumentNormalizer(),
        chunker=chunker,
        representations=RepresentationReuseService(representation_encoder),
        control=resources.control,
        raw_artifacts=RawDocumentArtifactRepository(writer, reader),
        canonical_artifacts=CanonicalDocumentArtifactRepository(writer, reader),
        comment_artifacts=CanonicalCommentArtifactRepository(writer, reader),
        table_artifacts=CanonicalTableArtifactRepository(writer, reader),
        chunk_writer=ChunkArtifactWriter(writer),
        chunk_reader=ChunkArtifactReader(reader),
        projection_artifacts=ProjectionArtifactRepository(writer, reader),
        vector_store=VectorProjectionStore(
            resources.vectors,
            VectorProjectionPolicy(dimension=3),
        ),
        graph_store=resources.graph,
    )


def build_release_service(resources: ReleaseResources) -> DocumentReleaseService:
    return DocumentReleaseService(build_dependencies(resources))


def build_relation_repair_service(
    resources: ReleaseResources,
    dependencies: DocumentReleaseDependencies,
    *,
    max_concurrency: int = 8,
) -> GraphRelationRepairService:
    return GraphRelationRepairService(
        control=resources.control,
        canonical_artifacts=dependencies.canonical_artifacts,
        chunk_reader=dependencies.chunk_reader,
        graph_store=resources.graph,
        max_concurrency=max_concurrency,
    )


def build_release_resources(
    control: IngestionControlPlaneDatabase,
) -> ReleaseResources:
    return ReleaseResources(
        control=control,
        store=MemoryObjectStore(),
        parser=TextParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )


def source_request(task_id: str) -> SourceIngestionRequest:
    return SourceIngestionRequest(
        tenant_id="default",
        task_id=task_id,
        connector_name="local-docs",
        connector_type=ConnectorType.LOCAL,
        connection_id="local-docs",
        source_scope_id="docs",
        configuration_fingerprint="local-config-v1",
        processing=processing_profile(),
    )


class SimpleTextRefiner:
    def __init__(self, token_counter) -> None:
        self._tokens = token_counter

    def split(self, request):
        from harborrag_core.contracts.chunking import (
            SourceSpan,
            SplitBoundaryKind,
            TextSplit,
        )

        return (
            TextSplit(
                content=request.content,
                token_count=self._tokens.count(request.content),
                source_span=request.source_span or SourceSpan(),
                boundary_kind=SplitBoundaryKind.PARAGRAPH,
                structural_path=request.structural_path,
            ),
        )
