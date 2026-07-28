from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.schemas.graph import GraphEdge, GraphNode, GraphSubgraph
from harborrag_core.schemas.ids import EntityId, RelationshipId, TenantId
from harborrag_core.schemas.vector import VectorSearchResult
from harborrag_engine.ingestion.indexing.config import IndexingConfig
from harborrag_runtime.retrieval import RetrievalResources, RuntimeRetrievalService


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
        self.queries = []

    async def search(self, query, *, context):
        self.queries.append((query, context))
        return [
            VectorSearchResult(
                id="point-1",
                score=0.9,
                raw_score=0.9,
                payload={
                    "artifact_id": "artifact-1",
                    "generation_id": "generation-1",
                    "chunk_revision_id": "revision-1",
                    "source_kind": "jira",
                    "chunk_role": "body",
                },
            )
        ]


class FakeGraphRepository:
    def __init__(self) -> None:
        self.queries = []

    async def expand(self, query, *, context):
        self.queries.append((query, context))
        return GraphSubgraph(
            nodes=[
                GraphNode(
                    id=EntityId("node-neighbour"),
                    tenant_id=context.tenant_id,
                    labels={"HarborEntity", "Chunk"},
                    properties={
                        "chunk_revision_id": "revision-2",
                        "is_active": True,
                    },
                )
            ],
            edges=[],
        )


class FakeChunkRepository:
    async def get_many(self, tenant_id, chunk_revision_ids):
        return tuple(
            SimpleNamespace(
                chunk_revision_id=revision,
                content=f"content for {revision}",
            )
            for revision in chunk_revision_ids
        )


def _resources(
    *,
    embed=None,
    vectors=None,
    graph=None,
) -> RetrievalResources:
    return RetrievalResources(
        embed_client=embed or FakeEmbedClient(),  # type: ignore[arg-type]
        vector_repository=vectors or FakeVectorRepository(),  # type: ignore[arg-type]
        graph_repository=graph or FakeGraphRepository(),  # type: ignore[arg-type]
        chunk_repository=FakeChunkRepository(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_retrieval_uses_sensitive_query_embeddings_and_graph_expansion() -> None:
    embed = FakeEmbedClient()
    vectors = FakeVectorRepository()
    graph = FakeGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(embed=embed, vectors=vectors, graph=graph),
        indexing_config=IndexingConfig("embed", 3, "chunks", "graph"),
    )

    report = await service.retrieve("release acceptance", tenant_id="tenant-1", top_k=2)

    assert {result.id for result in report.results} == {"revision-1", "revision-2"}
    assert report.diagnostics.vector_hits == 1
    assert report.diagnostics.graph_hits == 1
    assert report.diagnostics.graph_nodes == 1
    assert embed.requests[0].sensitive is True
    assert embed.requests[0].cacheable is False
    assert vectors.queries[0][0].top_k == 6
    assert vectors.queries[0][1].tenant_id == TenantId("tenant-1")
    assert graph.queries[0][0].max_depth == 2


@pytest.mark.asyncio
async def test_retrieval_closes_owned_resources_once() -> None:
    closed: list[str] = []

    async def close() -> None:
        closed.append("closed")

    service = RuntimeRetrievalService(
        resources=_resources(),
        indexing_config=IndexingConfig("embed", 3, "chunks", "graph"),
        close_resources=(close,),
    )

    await service.aclose()
    await service.aclose()

    assert closed == ["closed"]


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
        indexing_config=IndexingConfig("embed", 3, "chunks", "graph"),
    )

    with pytest.raises(ValueError):
        await service.retrieve(query, tenant_id=tenant_id, top_k=top_k)


def test_graph_nodes_are_ranked_by_seed_priority_and_distance() -> None:
    tenant_id = TenantId("tenant-1")
    seed_one = EntityId("seed-1")
    seed_two = EntityId("seed-2")
    neighbor = EntityId("neighbor")
    subgraph = GraphSubgraph(
        nodes=[
            _active_chunk(seed_two, tenant_id, "revision-2"),
            _active_chunk(neighbor, tenant_id, "revision-neighbor"),
            _active_chunk(seed_one, tenant_id, "revision-1"),
        ],
        edges=[
            GraphEdge(
                id=RelationshipId("edge-1"),
                tenant_id=tenant_id,
                source_id=seed_one,
                target_id=neighbor,
                relationship_type="NEXT_CHUNK",
            )
        ],
    )

    ranked = RuntimeRetrievalService._rank_graph_nodes(
        subgraph,
        [seed_one, seed_two],
    )

    assert [str(node.id) for node in ranked] == [
        "seed-1",
        "seed-2",
        "neighbor",
    ]


def _active_chunk(
    node_id: EntityId,
    tenant_id: TenantId,
    revision_id: str,
) -> GraphNode:
    return GraphNode(
        id=node_id,
        tenant_id=tenant_id,
        labels={"HarborEntity", "Chunk"},
        properties={
            "chunk_revision_id": revision_id,
            "is_active": True,
        },
    )
