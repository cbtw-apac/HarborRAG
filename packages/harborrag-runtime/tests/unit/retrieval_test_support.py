"""Small in-memory collaborators shared by runtime retrieval tests."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace

from harborrag_core.chunking import (
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    ArtifactReference,
    ChangeFingerprints,
    DocumentVersionSnapshot,
    DocumentVersionState,
    KnowledgeGraphTraversal,
    SparseEncoderProfile,
)
from harborrag_core.schemas.vector import VectorSearchResult
from harborrag_engine.ingestion import BM25SparseEncoder
from harborrag_runtime.retrieval import RetrievalPolicy, RetrievalResources


class FakeEmbedClient:
    def __init__(self) -> None:
        self.requests = []

    async def aembed(self, *, request):
        self.requests.append(request)
        return SimpleNamespace(embeddings=(SimpleNamespace(value=(1.0, 0.0, 0.0)),))

    async def aclose(self) -> None:
        return None


class FakeVectorRepository:
    def __init__(self) -> None:
        self.dense_queries = []
        self.sparse_queries = []
        self.hybrid_queries = []

    async def search(self, query, *, context):
        self.dense_queries.append((query, context))
        return self._results(query.index_name)

    async def sparse_search(self, query, *, context):
        self.sparse_queries.append((query, context))
        return self._results(query.index_name)

    async def hybrid_search(self, query, *, context):
        self.hybrid_queries.append((query, context))
        return self._results(query.index_name)

    @staticmethod
    def _results(collection: str) -> list[VectorSearchResult]:
        if "evidence" not in collection:
            return []
        return [
            VectorSearchResult(
                id="point-1",
                score=0.9,
                raw_score=0.9,
                payload={
                    "chunk_id": "chunk-1",
                    "document_id": "document-1",
                    "document_version_id": "version-1",
                    "record_kind": "evidence",
                    "chunk_kind": "text",
                    "connector_type": "local",
                    "content": "The activity timeout is 30 seconds.",
                },
            )
        ]


class FakeActiveVersions:
    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> dict[str, ActiveDocumentVersion]:
        del document_ids
        return {
            "document-1": ActiveDocumentVersion(
                document_id="document-1",
                document_version_id="version-1",
            )
        }


class FakeChunkReader:
    def __init__(self) -> None:
        self.references = []

    async def get_reference(self, reference, *, context):
        self.references.append((reference, context))
        content = "The activity timeout is 30 seconds."
        return ChunkRecord(
            strategy_version="strategy-1",
            logical_chunk_id="logical-chunk:1",
            chunk_id="chunk-1",
            connector_type=ConnectorType.LOCAL,
            document_kind=DocumentKind.LOCAL_FILE,
            record_kind=RecordKind.EVIDENCE,
            chunk_kind=ChunkKind.TEXT,
            tenant_id=str(context.tenant_id),
            connection_id="connection-1",
            source_scope_id="scope-1",
            source_item_id="guide.md",
            source_version="source-version-1",
            document_id="document-1",
            document_version_id="version-1",
            ordinal=0,
            content=content,
            embedding_text=content,
            search_text=content,
            token_count=6,
            content_hash="content-hash",
            security=ChunkSecurity(permission_set_id="permission-set:public"),
        )


class FakeDocumentSnapshots:
    def __init__(
        self, snapshots: dict[tuple[str, str], DocumentVersionSnapshot] | None = None
    ) -> None:
        self.snapshots = snapshots or {
            ("tenant-1", "document-1"): DocumentVersionSnapshot(
                document_id="document-1",
                document_version_id="version-1",
                fingerprints=ChangeFingerprints(
                    admission_change_key="change-1",
                    canonical_content_hash="hash-1",
                    retrieval_metadata_hash="hash-2",
                    processing_fingerprint="hash-3",
                ),
                state=DocumentVersionState.ACTIVE,
                canonical_artifact=ArtifactReference(
                    bucket="harborrag-artifacts",
                    key="canonical/document-1/version-1.json",
                    sha256="0" * 64,
                    byte_size=10,
                    media_type="application/json",
                ),
            )
        }

    async def active_snapshot_for_tenant(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> DocumentVersionSnapshot | None:
        return self.snapshots.get((tenant_id, document_id))


class FakeCanonicalDocuments:
    def __init__(self, document: Document | None = None) -> None:
        self.document = document or Document(
            id="document-1",
            title="Release guide",
            content=[
                DocumentElement(id="el-1", type="heading", content="Release guide"),
                DocumentElement(
                    id="el-2", type="paragraph", content="The activity timeout is 30 seconds."
                ),
            ],
            content_type="page",
            provenance=DocumentProvenance(source="confluence"),
        )
        self.requests = []

    async def get(self, reference, *, context):
        self.requests.append((reference, context))
        return self.document


class FakeGraphRepository:
    def __init__(self) -> None:
        self.queries = []

    async def traverse(self, start_node_key, **kwargs):
        self.queries.append((start_node_key, kwargs))
        return KnowledgeGraphTraversal(nodes=(), relations=())


class FailingGraphRepository(FakeGraphRepository):
    async def traverse(self, start_node_key, **kwargs):
        del start_node_key, kwargs
        raise ConnectionError("graph unavailable")


class MixedVectorRepository(FakeVectorRepository):
    @staticmethod
    def _results(collection: str) -> list[VectorSearchResult]:
        results = FakeVectorRepository._results(collection)
        if not results:
            return []
        malformed = results[0].model_copy(
            update={
                "id": "point-2",
                "payload": {
                    key: value for key, value in results[0].payload.items() if key != "content"
                },
            }
        )
        return [*results, malformed]


def resources(  # noqa: PLR0913 - test helper covers every retrieval resource explicitly
    *,
    embed=None,
    vectors=None,
    chunks=None,
    graph=None,
    active_versions=None,
    document_snapshots=None,
    canonical_documents=None,
) -> RetrievalResources:
    return RetrievalResources(
        embed_client=embed or FakeEmbedClient(),  # type: ignore[arg-type]
        vector_repository=vectors or FakeVectorRepository(),  # type: ignore[arg-type]
        active_versions=active_versions or FakeActiveVersions(),
        chunk_reader=chunks or FakeChunkReader(),  # type: ignore[arg-type]
        sparse_encoder=BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1")),
        graph_repository=graph,
        document_snapshots=document_snapshots,
        canonical_documents=canonical_documents,
    )


def policy() -> RetrievalPolicy:
    return RetrievalPolicy(embedding_model="embed", embedding_dimensions=3)
