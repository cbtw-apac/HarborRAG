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

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
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


def _document_node(node_key: str, node_kind: KnowledgeNodeKind, **overrides) -> GraphNodeRecord:
    defaults = {
        "node_key": node_key,
        "node_kind": node_kind,
        "entity_type": GraphEntityType.CHUNK,
        "logical_id": node_key,
        "ownership_scope": GraphOwnershipScope.DOCUMENT_VERSION,
        "owner_id": "tenant-1",
        "source_scope_id": "scope-1",
        "document_id": "document-1",
        "document_version_id": "version-1",
    }
    return GraphNodeRecord(**{**defaults, **overrides})


def _document_relation(relation_id: str, relation_type: RelationType, source: str, target: str) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=relation_id,
        relation_type=relation_type,
        source_node_key=source,
        target_node_key=target,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        document_id="document-1",
        document_version_id="version-1",
        source_relation_version="graph-v1",
        source_explicit=False,
    )


class DocumentGraphRepository(FakeGraphRepository):
    """Returns a real 2-hop neighborhood: chunk-1 -SUPPORTS-> structure-1 <-CONTAINS- docversion-1."""

    async def traverse(self, start_node_key, **kwargs):
        self.queries.append((start_node_key, kwargs))
        chunk = _document_node("chunk-1", KnowledgeNodeKind.CHUNK)
        structure = _document_node(
            "structure-1",
            KnowledgeNodeKind.STRUCTURE,
            entity_type=GraphEntityType.SECTION,
            title="Rollback steps",
        )
        document_version = _document_node(
            "docversion-1",
            KnowledgeNodeKind.DOCUMENT_VERSION,
            entity_type=GraphEntityType.DOCUMENT_VERSION,
            title="Runbook",
        )
        return KnowledgeGraphTraversal(
            nodes=(chunk, structure, document_version),
            relations=(
                _document_relation("relation-supports", RelationType.SUPPORTS, "chunk-1", "structure-1"),
                _document_relation("relation-contains", RelationType.CONTAINS, "docversion-1", "structure-1"),
            ),
        )


@pytest.mark.asyncio
async def test_graph_documents_reports_the_related_result_and_its_neighborhood() -> None:
    """graph_documents must say which vector result it came from and how, not just what it is."""

    graph = DocumentGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(graph=graph),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=True),
    )

    [document] = report.diagnostics.graph_documents
    assert document.document_id == "document-1"
    assert document.title == "Runbook"
    assert document.sections == ("Rollback steps",)

    [neighborhood] = document.related_results
    assert neighborhood.result_id == "chunk-1"
    assert {node["node_key"] for node in neighborhood.nodes} == {
        "chunk-1",
        "structure-1",
        "docversion-1",
    }
    assert {relation["relation_type"] for relation in neighborhood.relations} == {
        RelationType.SUPPORTS.value,
        RelationType.CONTAINS.value,
    }
