from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.errors import HarborStorageAuthorizationError
from harborrag_adapters.repositories.graph.falkordb import (
    client as falkordb_client_module,
)
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_adapters.repositories.graph.falkordb.repository import (
    FalkorDBGraphRepository,
)
from harborrag_adapters.repositories.graph.traversal import GraphTraversalSyntax
from harborrag_core.schemas.graph import GraphEdge, GraphExpansionQuery, GraphNode
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext


def _client_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "host": "localhost",
        "port": 6379,
        "username": None,
        "password": None,
        "graph_name": "harborrag",
        "ssl": False,
        "max_connections": 1,
        "connect_timeout_seconds": 1,
        "operation_timeout_seconds": 1,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# graph/traversal.py
# ---------------------------------------------------------------------------


def test_arrows_for_every_supported_direction() -> None:
    assert GraphTraversalSyntax.arrows("outgoing") == ("-", "->")
    assert GraphTraversalSyntax.arrows("incoming") == ("<-", "-")
    assert GraphTraversalSyntax.arrows("both") == ("-", "-")


def test_arrows_rejects_unsupported_direction() -> None:
    with pytest.raises(ValueError, match="unsupported graph direction"):
        GraphTraversalSyntax.arrows("sideways")


# ---------------------------------------------------------------------------
# graph/falkordb/mapping.py
# ---------------------------------------------------------------------------


def test_safe_identifier_accepts_valid_and_rejects_invalid_relationship_types() -> None:
    assert FalkorDBMapper.safe_identifier("knows") == "KNOWS"
    assert FalkorDBMapper.safe_identifier("Related_To_2") == "RELATED_TO_2"

    with pytest.raises(ValueError, match="relationship types must match"):
        FalkorDBMapper.safe_identifier("2bad")

    with pytest.raises(ValueError, match="relationship types must match"):
        FalkorDBMapper.safe_identifier("bad-id")


def test_node_maps_raw_dict_and_pops_harbor_metadata_fields() -> None:
    raw = {
        "id": "n1",
        "tenant_id": "tenant-a",
        "labels": ["Person", "Employee"],
        "confidence": 0.5,
        "provenance": {"source": "test"},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": None,
        "name": "Ada",
    }

    node = FalkorDBMapper.node(raw, "tenant-a")

    assert str(node.id) == "n1"
    assert node.labels == {"Person", "Employee"}
    assert node.confidence == 0.5
    assert node.provenance == {"source": "test"}
    assert node.properties == {"name": "Ada"}


def test_node_falls_back_to_raw_labels_attribute_when_missing_from_properties() -> None:
    class RawNode:
        def __init__(self) -> None:
            self.properties = {
                "id": "n1",
                "tenant_id": "tenant-a",
                "confidence": None,
                "provenance": {},
            }
            self.labels = ["Fallback"]

    node = FalkorDBMapper.node(RawNode(), "tenant-a")

    assert node.labels == {"Fallback"}


def test_edge_prefers_raw_relation_attribute_over_encoded_type_property() -> None:
    class RawEdge:
        def __init__(self) -> None:
            self.properties = {
                "id": "e1",
                "tenant_id": "tenant-a",
                "source_id": "n1",
                "target_id": "n2",
                "type": "IGNORED",
                "confidence": None,
                "provenance": {},
            }
            self.relation = "KNOWS"

    edge = FalkorDBMapper.edge(RawEdge(), "tenant-a")

    assert edge.relationship_type == "KNOWS"
    assert str(edge.source_id) == "n1"
    assert str(edge.target_id) == "n2"


def test_edge_falls_back_to_type_property_and_default_when_raw_is_plain_dict() -> None:
    with_type = {
        "id": "e1",
        "tenant_id": "tenant-a",
        "source_id": "n1",
        "target_id": "n2",
        "type": "KNOWS",
        "confidence": None,
        "provenance": {},
    }
    edge = FalkorDBMapper.edge(with_type, "tenant-a")
    assert edge.relationship_type == "KNOWS"

    without_type = {
        "id": "e2",
        "tenant_id": "tenant-a",
        "source_id": "n1",
        "target_id": "n2",
        "confidence": None,
        "provenance": {},
    }
    edge = FalkorDBMapper.edge(without_type, "tenant-a")
    assert edge.relationship_type == "RELATED_TO"


class _HeaderItem:
    def __init__(self, name: str | None) -> None:
        self.name = name


class _FakeQueryResult:
    def __init__(self, header: list[Any], result_set: list[list[Any]]) -> None:
        self.header = header
        self.result_set = result_set


def test_rows_resolves_header_names_from_tuples_attributes_and_fallback() -> None:
    result = _FakeQueryResult(
        header=[(1, "id"), _HeaderItem("name"), _HeaderItem(None)],
        result_set=[["n1", "Ada", "extra"]],
    )

    rows = FalkorDBMapper.rows(result)

    assert rows == [{"id": "n1", "name": "Ada", "column_2": "extra"}]


# ---------------------------------------------------------------------------
# graph/falkordb/client.py
# ---------------------------------------------------------------------------


class _FakeGraph:
    def __init__(self) -> None:
        self.query_calls: list[tuple[str, dict[str, Any]]] = []
        self.ro_query_calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, statement: str, params: dict[str, Any]) -> str:
        self.query_calls.append((statement, params))
        return "write-result"

    async def ro_query(self, statement: str, params: dict[str, Any]) -> str:
        self.ro_query_calls.append((statement, params))
        return "read-result"


class _FalkorDBWithGraph:
    def __init__(self, **options: Any) -> None:
        del options
        self.graph = _FakeGraph()

    def select_graph(self, name: str) -> _FakeGraph:
        del name
        return self.graph

    async def list_graphs(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_write_and_read_delegate_to_the_selected_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _FalkorDBWithGraph)
    client = FalkorDBClient(**_client_kwargs())

    await client.connect()
    write_result = await client.write("CREATE (n)", {"a": 1})
    read_result = await client.read("MATCH (n) RETURN n", {"b": 2})

    assert write_result == "write-result"
    assert read_result == "read-result"
    assert client.graph.query_calls == [("CREATE (n)", {"a": 1})]
    assert client.graph.ro_query_calls == [("MATCH (n) RETURN n", {"b": 2})]


def test_backend_property_reports_falkordb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", object)
    client = FalkorDBClient(**_client_kwargs())
    assert client.backend == "falkordb"


def test_raw_and_graph_properties_raise_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", object)
    client = FalkorDBClient(**_client_kwargs())
    with pytest.raises(RuntimeError):
        _ = client.raw
    with pytest.raises(RuntimeError):
        _ = client.graph


@pytest.mark.asyncio
async def test_close_without_connect_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", object)
    client = FalkorDBClient(**_client_kwargs())
    await client.close()


class _CountingFalkorDB:
    instances = 0

    def __init__(self, **options: Any) -> None:
        del options
        _CountingFalkorDB.instances += 1

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_connect_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _CountingFalkorDB.instances = 0
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _CountingFalkorDB)
    client = FalkorDBClient(**_client_kwargs())

    await client.connect()
    await client.connect()

    assert _CountingFalkorDB.instances == 1


class _FalkorDBPingFails:
    last_instance: _FalkorDBPingFails | None = None

    def __init__(self, **options: Any) -> None:
        del options
        self.closed = False
        _FalkorDBPingFails.last_instance = self

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        raise RuntimeError("ping failed")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_connect_failure_closes_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _FalkorDBPingFails)
    client = FalkorDBClient(**_client_kwargs())

    with pytest.raises(RuntimeError, match="ping failed"):
        await client.connect()

    assert _FalkorDBPingFails.last_instance is not None
    assert _FalkorDBPingFails.last_instance.closed is True
    with pytest.raises(RuntimeError):
        _ = client.raw


class _FalkorDBDirectClose:
    def __init__(self, **options: Any) -> None:
        del options
        self.closed = False

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_close_prefers_database_level_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _FalkorDBDirectClose)
    client = FalkorDBClient(**_client_kwargs())
    await client.connect()
    database = client.raw

    await client.close()

    assert database.closed is True
    with pytest.raises(RuntimeError):
        _ = client.raw


class _SyncCloseConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FalkorDBSyncConnectionCloseOnly:
    def __init__(self, **options: Any) -> None:
        del options
        self.connection = _SyncCloseConnection()

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_close_falls_back_to_synchronous_connection_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _FalkorDBSyncConnectionCloseOnly)
    client = FalkorDBClient(**_client_kwargs())
    await client.connect()
    connection = client.raw.connection

    await client.close()

    assert connection.closed is True


# ---------------------------------------------------------------------------
# graph/falkordb/repository.py
# ---------------------------------------------------------------------------


class FakeFalkorDBClient:
    def __init__(self) -> None:
        self.connected = False
        self.write_calls: list[tuple[str, dict[str, Any]]] = []
        self.read_calls: list[tuple[str, dict[str, Any]]] = []
        self.read_results: list[_FakeQueryResult] = []
        self.ping_error: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def ping(self) -> None:
        if self.ping_error is not None:
            raise self.ping_error

    async def write(self, statement: str, parameters: dict[str, Any]) -> None:
        self.write_calls.append((statement, dict(parameters)))

    async def read(self, statement: str, parameters: dict[str, Any]) -> _FakeQueryResult:
        self.read_calls.append((statement, dict(parameters)))
        return self.read_results.pop(0)


def _raw_node(entity_id: str, tenant_id: str, labels: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "id": entity_id,
        "tenant_id": tenant_id,
        "labels": labels,
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": None,
        **extra,
    }


def _raw_edge(
    edge_id: str,
    tenant_id: str,
    source_id: str,
    target_id: str,
    relationship_type: str = "RELATED_TO",
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "tenant_id": tenant_id,
        "source_id": source_id,
        "target_id": target_id,
        "type": relationship_type,
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": None,
    }


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
    assert all(
        parameters["tenant_id"] == "tenant-a" for _, parameters in client.write_calls
    )


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
        _FakeQueryResult(
            header=[_HeaderItem("node")],
            result_set=[[_raw_node("n1", "tenant-a", ["Person"])]],
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
    node_a = _raw_node("n1", "tenant-a", ["Person"])
    node_b = _raw_node("n2", "tenant-a", ["Person"])
    node_c = _raw_node("n3", "tenant-a", ["Person"])
    edge_ab = _raw_edge("e1", "tenant-a", "n1", "n2", "KNOWS")
    dangling_edge = _raw_edge("e2", "tenant-a", "n2", "n404", "KNOWS")
    client.read_results = [
        _FakeQueryResult(
            header=[_HeaderItem("path_nodes"), _HeaderItem("path_edges")],
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
    node_a = _raw_node("n1", "tenant-a", ["Person"])
    node_b = _raw_node("n2", "tenant-a", ["Person"])
    edge_ab = _raw_edge("e1", "tenant-a", "n1", "n2")
    client.read_results = [
        _FakeQueryResult(
            header=[_HeaderItem("path_nodes"), _HeaderItem("path_edges")],
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
    node_a = _raw_node("n1", "tenant-a", ["Person"])
    # max_nodes=5 -> path_limit = 20; return more rows than that so the
    # provider-side truncation flag is set purely from row count, and the
    # very first row's content ends up being the entire result (the loop
    # breaks immediately once `truncated` is already True).
    result_set = [[[node_a], []] for _ in range(25)]
    client.read_results = [
        _FakeQueryResult(
            header=[_HeaderItem("path_nodes"), _HeaderItem("path_edges")],
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
        _FakeQueryResult(
            header=[_HeaderItem("path_nodes"), _HeaderItem("path_edges")],
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


# ---------------------------------------------------------------------------
# Additional coverage: import guard and defensive close() branch
# ---------------------------------------------------------------------------


def test_falkordb_client_requires_sdk_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", None)

    with pytest.raises(ImportError, match="FalkorDB is not installed"):
        FalkorDBClient(**_client_kwargs())


class _FalkorDBWithoutAnyCloseMethod:
    def __init__(self, **options: Any) -> None:
        del options

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_close_is_a_noop_when_no_close_method_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _FalkorDBWithoutAnyCloseMethod)
    client = FalkorDBClient(**_client_kwargs())
    await client.connect()

    await client.close()

    with pytest.raises(RuntimeError):
        _ = client.raw


def test_node_preserves_a_truthy_valid_to_timestamp() -> None:
    raw = {
        "id": "n1",
        "tenant_id": "tenant-a",
        "labels": ["Person"],
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": "2024-06-01T00:00:00+00:00",
    }

    node = FalkorDBMapper.node(raw, "tenant-a")

    assert node.valid_to is not None
    assert node.valid_to.year == 2024
    assert node.valid_to.month == 6


def test_edge_preserves_a_truthy_valid_to_timestamp() -> None:
    raw = {
        "id": "e1",
        "tenant_id": "tenant-a",
        "source_id": "n1",
        "target_id": "n2",
        "type": "KNOWS",
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": "2024-06-01T00:00:00+00:00",
    }

    edge = FalkorDBMapper.edge(raw, "tenant-a")

    assert edge.valid_to is not None
    assert edge.valid_to.year == 2024
    assert edge.valid_to.month == 6
