from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from harborrag_core.chunking import (
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    KnowledgeGraphTraversal,
    SparseEncoderProfile,
)
from harborrag_core.retrieval import GraphNeighborhoodQuery
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.vector import VectorSearchResult
from harborrag_core.security import AccessContext
from harborrag_engine.ingestion import BM25SparseEncoder
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.retrieval import (
    RetrievalOptions,
    RetrievalPolicy,
    RetrievalResources,
    RuntimeRetrievalService,
)


class FakeEmbedClient:
    def __init__(self) -> None:
        self.requests = []

    async def aembed(self, *, request):
        self.requests.append(request)
        return SimpleNamespace(
            embeddings=(SimpleNamespace(value=(1.0, 0.0, 0.0)),),
        )

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


def _resources(
    *,
    embed=None,
    vectors=None,
    chunks=None,
    graph=None,
) -> RetrievalResources:
    return RetrievalResources(
        embed_client=embed or FakeEmbedClient(),  # type: ignore[arg-type]
        vector_repository=vectors or FakeVectorRepository(),  # type: ignore[arg-type]
        active_versions=FakeActiveVersions(),
        chunk_reader=chunks or FakeChunkReader(),  # type: ignore[arg-type]
        sparse_encoder=BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1")),
        graph_repository=graph,
    )


def _policy() -> RetrievalPolicy:
    return RetrievalPolicy(
        embedding_model="embed",
        embedding_dimensions=3,
    )


@pytest.mark.asyncio
async def test_hybrid_retrieval_returns_vector_payload_content() -> None:
    embed = FakeEmbedClient()
    vectors = FakeVectorRepository()
    chunks = FakeChunkReader()
    graph = FakeGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(
            embed=embed,
            vectors=vectors,
            chunks=chunks,
            graph=graph,
        ),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        top_k=2,
        options=RetrievalOptions(observe_graph=True),
    )

    assert [result.id for result in report.results] == ["chunk-1"]
    assert report.results[0].text == "The activity timeout is 30 seconds."
    assert report.lane == RetrievalLane.HYBRID
    assert report.diagnostics.candidate_hits == 1
    assert report.diagnostics.stale_candidates == 0
    assert embed.requests[0].sensitive is True
    assert embed.requests[0].cacheable is False
    assert len(vectors.hybrid_queries) == 1
    assert chunks.references == []
    assert len(graph.queries) == 1
    assert graph.queries[0][0] == "chunk-1"


@pytest.mark.asyncio
async def test_sparse_retrieval_does_not_call_dense_encoder() -> None:
    embed = FakeEmbedClient()
    vectors = FakeVectorRepository()
    service = RuntimeRetrievalService(
        resources=_resources(embed=embed, vectors=vectors),
        policy=_policy(),
    )

    report = await service.retrieve(
        "HARBOR-42",
        tenant_id="tenant-1",
        top_k=1,
        options=RetrievalOptions(
            lane=RetrievalLane.SPARSE,
            observe_graph=False,
        ),
    )

    assert [result.id for result in report.results] == ["chunk-1"]
    assert not embed.requests
    assert not vectors.dense_queries
    assert not vectors.hybrid_queries
    assert len(vectors.sparse_queries) == 1
    assert all(query.filters is None for query, _ in vectors.sparse_queries)


@pytest.mark.asyncio
async def test_malformed_candidate_is_skipped_without_losing_valid_results() -> None:
    service = RuntimeRetrievalService(
        resources=_resources(vectors=MixedVectorRepository()),
        policy=_policy(),
    )

    report = await service.retrieve("release", tenant_id="tenant-1")

    assert [result.id for result in report.results] == ["chunk-1"]
    assert report.diagnostics.malformed_candidates == 1


@pytest.mark.asyncio
async def test_optional_graph_observation_failure_does_not_fail_retrieval() -> None:
    service = RuntimeRetrievalService(
        resources=_resources(graph=FailingGraphRepository()),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=True),
    )

    assert [result.id for result in report.results] == ["chunk-1"]
    assert report.diagnostics.graph_nodes == 0
    assert report.diagnostics.graph_relations == 0


@pytest.mark.asyncio
async def test_retrieval_closes_owned_resources_once() -> None:
    closed: list[str] = []

    async def close() -> None:
        closed.append("closed")

    service = RuntimeRetrievalService(
        resources=_resources(),
        policy=_policy(),
        close_resources=(close,),
    )

    await service.aclose()
    await service.aclose()

    assert closed == ["closed"]


@pytest.mark.asyncio
async def test_retrieval_attempts_every_close_and_allows_retry_after_failure() -> None:
    closed: list[str] = []
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary close failure")
        closed.append("flaky")

    async def healthy() -> None:
        closed.append("healthy")

    service = RuntimeRetrievalService(
        resources=_resources(),
        policy=_policy(),
        close_resources=(healthy, flaky),
    )

    with pytest.raises(ExceptionGroup):
        await service.aclose()
    assert closed == ["healthy"]

    await service.aclose()
    assert closed == ["healthy", "flaky", "healthy"]


@pytest.mark.parametrize(
    ("query", "tenant_id", "top_k"),
    [
        ("", "tenant", 1),
        ("query", "", 1),
        ("query", "tenant", 0),
        ("query", "tenant", 101),
    ],
)
@pytest.mark.asyncio
async def test_retrieval_rejects_invalid_public_inputs(query, tenant_id, top_k) -> None:
    service = RuntimeRetrievalService(
        resources=_resources(),
        policy=_policy(),
    )

    with pytest.raises(ValueError):
        await service.retrieve(query, tenant_id=tenant_id, top_k=top_k)


class SeedRecordingGraphRepository(FakeGraphRepository):
    """Records the seeds a neighborhood expansion was asked to grow from."""

    def __init__(self) -> None:
        super().__init__()
        self.expanded: list[str] = []

    async def expand_subgraph(self, query, *, context):
        del context
        self.expanded.append(query.start_node)
        return KnowledgeGraphTraversal(nodes=(), relations=())


@pytest.mark.asyncio
async def test_neighborhood_seeds_the_graph_with_vector_chunk_ids() -> None:
    """The chunk_id a vector hit carries is the Chunk node key the graph expands from.

    This is the only bridge into the graph -- every other selector is an opaque hash, an
    internal id, or a title that is null on chunk nodes -- so the identity of these two
    strings is the assumption the whole graph entry path rests on.
    """

    graph = SeedRecordingGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(graph=graph),
        policy=_policy(),
    )

    seeds, result = await service.search_graph_neighborhood(
        GraphNeighborhoodQuery(query="how long is the activity timeout?"),
        access=AccessContext(principal_id="reader-1", tenant_id=TenantId("tenant-1")),
    )

    assert seeds == ("chunk-1",)
    assert graph.expanded == ["chunk-1"]
    assert result.graph.nodes == ()


@pytest.mark.asyncio
async def test_neighborhood_does_not_pay_for_graph_observation_while_seeding() -> None:
    """The seeding search must not also trigger the observer -- that would double the work."""

    graph = SeedRecordingGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(graph=graph),
        policy=_policy(),
    )

    await service.search_graph_neighborhood(
        GraphNeighborhoodQuery(query="anything"),
        access=AccessContext(principal_id="reader-1", tenant_id=TenantId("tenant-1")),
    )

    assert graph.queries == []
