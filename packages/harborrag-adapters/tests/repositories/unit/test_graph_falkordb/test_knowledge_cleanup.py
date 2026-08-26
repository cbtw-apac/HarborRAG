from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import GraphSubgraphQuery
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem


def repository(client: FakeFalkorDBClient) -> FalkorKnowledgeGraphRepository:
    return FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(),
        client=client,  # type: ignore[arg-type]
    )


def nodes() -> tuple[GraphNodeRecord, GraphNodeRecord]:
    return (
        GraphNodeRecord(
            node_key="node-document",
            node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
            entity_type=GraphEntityType.DOCUMENT_VERSION,
            logical_id="version-1",
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            owner_id="tenant-1",
            document_id="document-1",
            document_version_id="version-1",
            source_scope_id="scope-1",
            title="Document",
        ),
        GraphNodeRecord(
            node_key="node-section",
            node_kind=KnowledgeNodeKind.STRUCTURE,
            entity_type=GraphEntityType.SECTION,
            logical_id="section-1",
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            owner_id="tenant-1",
            document_id="document-1",
            document_version_id="version-1",
            source_scope_id="scope-1",
            title="Operations",
        ),
    )


def relation() -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id="relation-1",
        relation_type=RelationType.CONTAINS,
        source_node_key="node-document",
        target_node_key="node-section",
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        document_id="document-1",
        document_version_id="version-1",
        source_relation_version="graph-v1",
        source_explicit=False,
    )


@pytest.mark.asyncio
async def test_subgraph_search_applies_bounded_filtered_traversal() -> None:
    client = FakeFalkorDBClient()
    document, section = nodes()
    client.read_results = [
        FakeQueryResult([HeaderItem("node")], [[document.model_dump(mode="json")]]),
        FakeQueryResult(
            [HeaderItem("relation"), HeaderItem("related")],
            [[relation().model_dump(mode="json"), section.model_dump(mode="json")]],
        ),
    ]

    result = await repository(client).expand_subgraph(
        GraphSubgraphQuery(
            start_node="node-document",
            relationship_types=(RelationType.CONTAINS,),
            max_depth=1,
            max_nodes=10,
        ),
        context=StorageOperationContext.system("tenant-1"),
    )

    _, start_parameters = client.read_calls[0]
    assert start_parameters["start_node"] == "node-document"
    _, level_parameters = client.read_calls[1]
    assert level_parameters["relationship_types"] == ["contains"]
    assert len(result.nodes) == 2
    assert len(result.relations) == 1


@pytest.mark.asyncio
async def test_tenant_projection_inventory_and_delete_are_tenant_scoped() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult([HeaderItem("item_count")], [[12]]),
        FakeQueryResult([HeaderItem("item_count")], [[8]]),
    ]
    graph = repository(client)
    context = StorageOperationContext.system("tenant-1")

    counts = await graph.tenant_projection_counts(context=context)
    await graph.delete_tenant_projection(context=context)

    assert counts == (12, 8)
    assert len(client.write_calls) == 2
    for statement, parameters in (*client.read_calls, *client.write_calls):
        assert "$tenant_id" in statement
        assert parameters["tenant_id"] == "tenant-1"
    assert "DETACH DELETE node" in client.write_calls[1][0]


@pytest.mark.asyncio
async def test_version_cleanup_deletes_only_version_owned_v2_records() -> None:
    client = FakeFalkorDBClient()

    await repository(client).delete_version(
        "version-1",
        context=StorageOperationContext.system("tenant-1"),
    )

    assert len(client.write_calls) == 3
    for statement, parameters in client.write_calls[:2]:
        assert "ownership_scope = 'DOCUMENT_VERSION'" in statement
        assert "graph_schema_version = $graph_schema_version" in statement
        assert parameters["document_version_id"] == "version-1"
    assert "NOT (node)--()" in client.write_calls[2][0]


@pytest.mark.asyncio
async def test_source_scope_cleanup_is_connection_scoped() -> None:
    client = FakeFalkorDBClient()

    await repository(client).delete_source_scope(
        "scope-1",
        context=StorageOperationContext.system("tenant-1"),
    )

    assert len(client.write_calls) == 2
    assert all("source_scope_id = $source_scope_id" in call[0] for call in client.write_calls)
    assert all(call[1]["source_scope_id"] == "scope-1" for call in client.write_calls)


@pytest.mark.asyncio
async def test_schema_v2_migration_gate_and_legacy_cleanup_are_isolated() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult([HeaderItem("chunk_id")], []),
        FakeQueryResult([HeaderItem("node_key")], []),
        FakeQueryResult([HeaderItem("item_count")], [[0], [0]]),
    ]
    graph = repository(client)
    context = StorageOperationContext.system("tenant-1")

    verification = await graph.verify_schema_v2_migration(
        evidence_chunk_ids=("chunk-1",),
        active_source_item_node_keys=("source-item-1",),
        context=context,
    )
    await graph.delete_legacy_tenant_projection(context=context)

    assert verification.valid is True
    assert len(client.write_calls) == 2
    assert all("<> $graph_schema_version" in statement for statement, _ in client.write_calls)


@pytest.mark.parametrize(
    ("message", "swallowed"),
    [
        ("Index already exists", True),
        ("attribute 'node_key' is already indexed", True),
        ("already-exists", True),
        ("AlreadyExists", True),
        ("ALREADY EXISTS", True),
        ("Index does not exist", False),
        ("graph does not exist", False),
        ("connection refused", False),
    ],
)
@pytest.mark.asyncio
async def test_provisioning_swallows_idempotent_ddl_but_reraises_real_failures(
    message: str,
    swallowed: bool,
) -> None:
    class FailingClient(FakeFalkorDBClient):
        async def write(self, statement: str, parameters: dict[str, object]) -> None:
            raise RuntimeError(message)

        async def create_unique_node_constraint(
            self, *, label: str, properties: tuple[str, ...]
        ) -> None:
            raise RuntimeError(message)

    graph = repository(FailingClient())
    if swallowed:
        await graph.provision()
        return
    with pytest.raises(RuntimeError, match=message):
        await graph.provision()


@pytest.mark.asyncio
async def test_relation_cleanup_deletes_relations_and_never_a_node() -> None:
    """One statement per relationship type, and not one of them deletes a node.

    The label is named because the only relation index provisioning creates is
    per-relationship-label, so an untyped match would scan every relationship in the
    graph.

    That no node is deleted is the load-bearing half. Removing a far end left edgeless
    means testing its degree in one transaction and deleting it in another, while
    `write_projection` stages nodes before their relations -- so a placeholder another
    projection has just staged reads as edgeless, and deleting it makes that projection
    write no relation and fail its own verification. Scoping the test to the keys just
    retracted does not help: a placeholder shared by several linking documents is exactly
    the node the other writer is staging. A deletion that does not exist cannot race, so
    the stub is left for the tenant-wide prune that version and source-item cleanup run.
    """

    client = FakeFalkorDBClient()
    links_to = relation().model_copy(
        update={"relation_id": "relation-2", "relation_type": RelationType.LINKS_TO}
    )

    await repository(client).delete_relations(
        (relation(), links_to),
        context=StorageOperationContext.system("tenant-1"),
    )

    statements = [statement for statement, _ in client.write_calls]
    assert len(statements) == 2
    assert "[relation:CONTAINS]" in statements[0]
    assert "[relation:LINKS_TO]" in statements[1]
    for statement, parameters in client.write_calls:
        assert "relation.tenant_id = $tenant_id" in statement
        assert "relation.graph_schema_version = $graph_schema_version" in statement
        assert parameters["tenant_id"] == "tenant-1"
        assert "DELETE relation" in statement
        assert "DELETE node" not in statement
        assert "DETACH DELETE" not in statement
    assert client.write_calls[0][1]["relation_ids"] == ["relation-1"]
    assert client.write_calls[1][1]["relation_ids"] == ["relation-2"]


@pytest.mark.asyncio
async def test_relation_cleanup_rejects_another_tenants_relation() -> None:
    client = FakeFalkorDBClient()

    with pytest.raises(ValueError, match="owner does not match"):
        await repository(client).delete_relations(
            (relation(),),
            context=StorageOperationContext.system("tenant-2"),
        )

    assert client.write_calls == []


@pytest.mark.asyncio
async def test_relation_cleanup_with_nothing_to_retract_does_not_touch_the_graph() -> None:
    """Repair calls this on every document it repairs, most of which supersede nothing."""

    client = FakeFalkorDBClient()

    await repository(client).delete_relations(
        (),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert client.write_calls == []
