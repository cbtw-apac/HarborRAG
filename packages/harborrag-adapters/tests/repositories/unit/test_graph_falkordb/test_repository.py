from __future__ import annotations

import pytest

from harborrag_adapters.repositories.errors import HarborStorageAuthorizationError
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.repository import (
    FalkorDBGraphRepository,
)
from harborrag_core.schemas.graph import GraphEdge, GraphExpansionQuery, GraphNode
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem, raw_edge, raw_node


def _make_repository(client: FakeFalkorDBClient) -> FalkorDBGraphRepository:
    return FalkorDBGraphRepository(FalkorDBGraphConfig(), client=client)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_activate_generation_retires_previous_and_exposes_current() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")

    await repository.activate_generation(
        artifact_id="artifact-1",
        generation_id="generation-2",
        previous_generation_id="generation-1",
        context=context,
    )

    assert len(client.write_calls) == 4
    states = [parameters["index_state"] for _, parameters in client.write_calls]
    assert states == ["retired", "retired", "active", "active"]
    assert all(parameters["tenant_id"] == "tenant-a" for _, parameters in client.write_calls)


@pytest.mark.asyncio
async def test_connect_initializes_schema_indexes() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)

    await repository.connect()

    assert client.connected is True
    assert len(client.write_calls) == 5
    assert all("CREATE INDEX" in statement for statement, _ in client.write_calls)


@pytest.mark.asyncio
async def test_close_delegates_to_database_client() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    await repository.connect()

    await repository.close()

    assert client.connected is False


@pytest.mark.asyncio
async def test_health_reports_healthy_when_ping_succeeds() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)

    health = await repository.health()

    assert health.status == HealthStatus.HEALTHY
    assert health.family.value == "graph"


@pytest.mark.asyncio
async def test_health_reports_unhealthy_when_ping_raises() -> None:
    client = FakeFalkorDBClient()
    client.ping_error = RuntimeError("down")
    repository = _make_repository(client)

    health = await repository.health()

    assert health.status == HealthStatus.UNHEALTHY
    assert health.details["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_upsert_nodes_writes_encoded_rows_for_matching_tenant() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    node = GraphNode(id="n1", tenant_id="tenant-a", labels={"Person"}, properties={"name": "Ada"})

    await repository.upsert_nodes([node], context=context)

    assert len(client.write_calls) == 1
    statement, params = client.write_calls[0]
    assert "MERGE (n:HarborEntity" in statement
    [row] = params["rows"]
    assert row["id"] == str(node.id)
    assert row["tenant_id"] == "tenant-a"
    assert row["labels"] == ["Person"]
    assert row["properties"]["name"] == "Ada"


@pytest.mark.asyncio
async def test_upsert_nodes_rejects_cross_tenant_node() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    node = GraphNode(id="n1", tenant_id="tenant-b")

    with pytest.raises(HarborStorageAuthorizationError):
        await repository.upsert_nodes([node], context=context)

    assert client.write_calls == []


@pytest.mark.asyncio
async def test_upsert_edges_groups_by_relationship_type_and_writes_each_group() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    edges = [
        GraphEdge(
            id="e1",
            tenant_id="tenant-a",
            source_id="n1",
            target_id="n2",
            relationship_type="knows",
        ),
        GraphEdge(
            id="e2",
            tenant_id="tenant-a",
            source_id="n2",
            target_id="n3",
            relationship_type="manages",
        ),
    ]

    await repository.upsert_edges(edges, context=context)

    assert len(client.write_calls) == 2
    relationship_types = {
        statement.split("MERGE (source)-[r:")[1].split(" ")[0]
        for statement, _ in client.write_calls
    }
    assert relationship_types == {"KNOWS", "MANAGES"}


@pytest.mark.asyncio
async def test_upsert_edges_rejects_cross_tenant_edge() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    edge = GraphEdge(
        id="e1",
        tenant_id="tenant-b",
        source_id="n1",
        target_id="n2",
        relationship_type="knows",
    )

    with pytest.raises(HarborStorageAuthorizationError):
        await repository.upsert_edges([edge], context=context)

    assert client.write_calls == []


@pytest.mark.asyncio
async def test_get_nodes_maps_provider_rows_into_graph_nodes() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult(
            header=[HeaderItem("node")],
            result_set=[[raw_node("n1", "tenant-a", ["Person"])]],
        )
    ]
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")

    nodes = await repository.get_nodes(["n1"], context=context)

    assert [str(node.id) for node in nodes] == ["n1"]
    assert client.read_calls[0][1] == {"tenant_id": "tenant-a", "ids": ["n1"]}


@pytest.mark.asyncio
async def test_delete_nodes_sends_tenant_scoped_delete_statement() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")

    await repository.delete_nodes(["n1", "n2"], context=context)

    assert len(client.write_calls) == 1
    statement, params = client.write_calls[0]
    assert "DETACH DELETE n" in statement
    assert params == {"tenant_id": "tenant-a", "ids": ["n1", "n2"]}


@pytest.mark.asyncio
async def test_delete_edges_sends_tenant_scoped_delete_statement() -> None:
    client = FakeFalkorDBClient()
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")

    await repository.delete_edges(["e1"], context=context)

    assert len(client.write_calls) == 1
    statement, params = client.write_calls[0]
    assert "DELETE r" in statement
    assert params == {"tenant_id": "tenant-a", "ids": ["e1"]}


@pytest.mark.asyncio
async def test_expand_returns_untruncated_subgraph_with_filtered_edges() -> None:
    client = FakeFalkorDBClient()
    node_a = raw_node("n1", "tenant-a", ["Person"])
    node_b = raw_node("n2", "tenant-a", ["Person"])
    node_c = raw_node("n3", "tenant-a", ["Person"])
    edge_ab = raw_edge("e1", "tenant-a", "n1", "n2", "KNOWS")
    dangling_edge = raw_edge("e2", "tenant-a", "n2", "n404", "KNOWS")
    client.read_results = [
        FakeQueryResult(
            header=[HeaderItem("path_nodes"), HeaderItem("path_edges")],
            result_set=[
                [[node_a, node_b], [edge_ab]],
                [[node_b, node_c], [dangling_edge]],
            ],
        )
    ]
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    query = GraphExpansionQuery(
        start_nodes=["n1"],
        max_depth=2,
        max_nodes=200,
        direction="both",
        relationship_types=["knows"],
    )

    subgraph = await repository.expand(query, context=context)

    assert subgraph.truncated is False
    assert {str(node.id) for node in subgraph.nodes} == {"n1", "n2", "n3"}
    assert [str(edge.id) for edge in subgraph.edges] == ["e1"]


@pytest.mark.asyncio
async def test_expand_truncates_when_max_nodes_reached_mid_row() -> None:
    client = FakeFalkorDBClient()
    node_a = raw_node("n1", "tenant-a", ["Person"])
    node_b = raw_node("n2", "tenant-a", ["Person"])
    edge_ab = raw_edge("e1", "tenant-a", "n1", "n2")
    client.read_results = [
        FakeQueryResult(
            header=[HeaderItem("path_nodes"), HeaderItem("path_edges")],
            result_set=[[[node_a, node_b], [edge_ab]]],
        )
    ]
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    query = GraphExpansionQuery(start_nodes=["n1"], max_depth=1, max_nodes=1, direction="outgoing")

    subgraph = await repository.expand(query, context=context)

    assert subgraph.truncated is True
    assert {str(node.id) for node in subgraph.nodes} == {"n1"}
    # The edge's target ("n2") never made it into the truncated node set, so
    # the edge must be dropped from the returned subgraph.
    assert subgraph.edges == []


@pytest.mark.asyncio
async def test_expand_truncates_when_more_rows_than_the_path_limit_are_returned() -> None:
    client = FakeFalkorDBClient()
    node_a = raw_node("n1", "tenant-a", ["Person"])
    # max_nodes=5 -> path_limit = 20; return more rows than that so the
    # provider-side truncation flag is set purely from row count, and the
    # very first row's content ends up being the entire result (the loop
    # breaks immediately once `truncated` is already True).
    result_set = [[[node_a], []] for _ in range(25)]
    client.read_results = [
        FakeQueryResult(
            header=[HeaderItem("path_nodes"), HeaderItem("path_edges")],
            result_set=result_set,
        )
    ]
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    query = GraphExpansionQuery(start_nodes=["n1"], max_depth=1, max_nodes=5, direction="both")

    subgraph = await repository.expand(query, context=context)

    assert subgraph.truncated is True
    assert {str(node.id) for node in subgraph.nodes} == {"n1"}


@pytest.mark.asyncio
async def test_expand_with_no_relationship_types_omits_the_selector() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult(
            header=[HeaderItem("path_nodes"), HeaderItem("path_edges")],
            result_set=[],
        )
    ]
    repository = _make_repository(client)
    context = StorageOperationContext(tenant_id="tenant-a")
    query = GraphExpansionQuery(start_nodes=["n1"], direction="incoming")

    subgraph = await repository.expand(query, context=context)

    statement, _ = client.read_calls[0]
    assert "<-[r*1.." in statement
    assert subgraph.nodes == []
    assert subgraph.edges == []
    assert subgraph.truncated is False
