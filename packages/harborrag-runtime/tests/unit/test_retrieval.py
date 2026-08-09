from __future__ import annotations

import pytest
from retrieval_test_support import (
    FailingGraphRepository,
    FakeChunkReader,
    FakeEmbedClient,
    FakeGraphRepository,
    FakeVectorRepository,
    MixedVectorRepository,
)
from retrieval_test_support import (
    policy as _policy,
)
from retrieval_test_support import (
    resources as _resources,
)

from harborrag_core.ingestion import KnowledgeGraphTraversal
from harborrag_core.retrieval import GraphNeighborhoodQuery
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.retrieval import (
    RetrievalOptions,
    RuntimeRetrievalService,
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
