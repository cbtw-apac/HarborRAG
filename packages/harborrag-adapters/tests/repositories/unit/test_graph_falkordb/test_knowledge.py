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
from harborrag_core.retrieval import (
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
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
            title="Title with ') DETACH DELETE node //",
            attributes={
                "connector_type": "local",
                "source_item_id": "docs/release.md",
                "source_uri": "file:///docs/release.md",
            },
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
async def test_provision_creates_exact_indexes_and_unique_node_constraint() -> None:
    client = FakeFalkorDBClient()

    await repository(client).connect()

    assert client.connected is True
    assert all("CREATE INDEX" in statement for statement, _ in client.write_calls)
    node_indexes = {
        statement for statement, _ in client.write_calls if "(node:KnowledgeNode)" in statement
    }
    relation_indexes = {
        statement for statement, _ in client.write_calls if "-[relation:" in statement
    }
    assert len(node_indexes) + len(relation_indexes) == len(client.write_calls)
    # tenant_id is filtered by every read, delete, and count, so it must be indexed.
    assert "CREATE INDEX FOR (node:KnowledgeNode) ON (node.tenant_id)" in node_indexes
    # owner_id duplicates tenant_id and is filtered by no query; indexing it is pure cost.
    assert not any("owner_id" in statement for statement in node_indexes)
    # Relationship predicates were unindexed scans before; each written type is covered.
    assert relation_indexes
    assert all("ON (relation.tenant_id)" in statement for statement in relation_indexes)


@pytest.mark.asyncio
async def test_provision_replaces_the_pre_tenancy_uniqueness_constraint() -> None:
    client = FakeFalkorDBClient()

    await repository(client).connect()

    # The constraint must match the merge identity exactly. Keyed on node_key alone it
    # would reject the second tenant of a shared node_key, which is the write that the
    # tenant merge key exists to permit.
    assert client.constraint_calls == [
        ("KnowledgeNode", ("node_key", "graph_schema_version", "tenant_id"))
    ]
    assert client.dropped_constraint_calls == [("KnowledgeNode", ("node_key",))]


@pytest.mark.asyncio
async def test_node_merge_is_keyed_by_tenant_so_a_shared_node_key_cannot_collide() -> None:
    # Version-owned node keys (DocumentVersion, Structure, Chunk) do not hash the tenant,
    # so an identical node_key reaching two tenants is representable. Without tenant_id in
    # the merge key the second write would overwrite the first tenant's node.
    client = FakeFalkorDBClient()
    graph = repository(client)

    for tenant_id in ("tenant-1", "tenant-2"):
        shared = tuple(
            node.model_copy(update={"owner_id": tenant_id}) for node in nodes()
        )
        await graph.write_projection(
            shared,
            (),
            context=StorageOperationContext.system(tenant_id=tenant_id),
        )

    node_statements = [
        statement for statement, _ in client.write_calls if "KnowledgeNode:" in statement
    ]
    assert node_statements
    assert all("tenant_id: row.tenant_id" in statement for statement in node_statements)
    written_tenants = {
        row["tenant_id"]
        for _, parameters in client.write_calls
        for row in parameters.get("rows", ())
    }
    assert written_tenants == {"tenant-1", "tenant-2"}


@pytest.mark.asyncio
async def test_node_writes_replace_properties_so_dropped_fields_cannot_persist() -> None:
    # SET node += row patches; a property removed from a later projection would survive
    # forever. SET node = row replaces the map, which is what makes re-projection honest.
    client = FakeFalkorDBClient()
    graph = repository(client)
    context = StorageOperationContext.system(tenant_id="tenant-1")

    await graph.write_projection(nodes(), (relation(),), context=context)

    for statement, _ in client.write_calls:
        if "KnowledgeNode:" in statement:
            assert "SET node = row" in statement
            assert "SET node += row" not in statement
        if "MERGE (source)-[relation:" in statement:
            assert "SET relation = row" in statement
            assert "SET relation += row" not in statement


@pytest.mark.asyncio
async def test_replayed_writes_use_merge_and_keep_source_values_parameterized() -> None:
    client = FakeFalkorDBClient()
    graph = repository(client)
    context = StorageOperationContext.system(tenant_id="tenant-1")

    await graph.write_projection(nodes(), (relation(),), context=context)
    await graph.write_projection(nodes(), (relation(),), context=context)

    assert len(client.write_calls) == 6
    node_statements = [
        statement for statement, _ in client.write_calls if "KnowledgeNode:" in statement
    ]
    relation_statements = [
        statement for statement, _ in client.write_calls if "relation:" in statement
    ]
    assert all("MERGE" in statement for statement in (*node_statements, *relation_statements))
    assert all("DETACH DELETE" not in statement for statement in node_statements)
    assert any(
        row["title"] == "Title with ') DETACH DELETE node //"
        for _, parameters in client.write_calls
        for row in parameters.get("rows", ())
    )
    assert any(
        "content_preview" not in row
        and "file:///docs/release.md" in row["attributes"]
        and "local" in row["attributes"]
        for _, parameters in client.write_calls
        for row in parameters.get("rows", ())
    )


@pytest.mark.asyncio
async def test_verification_checks_endpoints_and_duplicate_identities() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("node_key"), HeaderItem("occurrences")],
            [["node-document", 1], ["node-section", 1]],
        ),
        FakeQueryResult(
            [
                HeaderItem("relation_id"),
                HeaderItem("source_node_key"),
                HeaderItem("target_node_key"),
                HeaderItem("occurrences"),
            ],
            [["relation-1", "node-document", "node-section", 1]],
        ),
    ]

    result = await repository(client).verify_projection(
        nodes(),
        (relation(),),
        context=StorageOperationContext.system(tenant_id="tenant-1"),
    )

    assert result.valid is True
    assert result.actual_node_count == 2
    assert result.actual_relation_count == 1


@pytest.mark.asyncio
async def test_partial_graph_readback_prevents_verification() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("node_key"), HeaderItem("occurrences")],
            [["node-document", 1]],
        ),
        FakeQueryResult(
            [
                HeaderItem("relation_id"),
                HeaderItem("source_node_key"),
                HeaderItem("target_node_key"),
                HeaderItem("occurrences"),
            ],
            [],
        ),
    ]

    result = await repository(client).verify_projection(
        nodes(),
        (relation(),),
        context=StorageOperationContext.system(tenant_id="tenant-1"),
    )

    assert result.valid is False
    assert result.missing_node_keys == ("node-section",)
    assert result.missing_relation_ids == ("relation-1",)


@pytest.mark.asyncio
async def test_triplet_search_is_parameterized_and_tenant_scoped() -> None:
    client = FakeFalkorDBClient()
    subject, object_node = nodes()
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("subject"), HeaderItem("predicate"), HeaderItem("object")],
            [
                [
                    subject.model_dump(mode="json"),
                    relation().model_dump(mode="json"),
                    object_node.model_dump(mode="json"),
                ]
            ],
        )
    ]

    result = await repository(client).search_triplets(
        GraphTripletQuery(subject="node-document", limit=2),
        context=StorageOperationContext.system("tenant-1"),
    )

    statement, parameters = client.read_calls[0]
    assert "subject.tenant_id = $tenant_id" in statement
    assert "node-document" not in statement
    assert parameters["subject"] == "node-document"
    assert parameters["tenant_id"] == "tenant-1"
    assert result.triplets[0].predicate.relation_id == "relation-1"


@pytest.mark.asyncio
async def test_path_search_returns_explicit_canonical_paths() -> None:
    client = FakeFalkorDBClient()
    path_nodes = [node.model_dump(mode="json") for node in nodes()]
    path_relations = [relation().model_dump(mode="json")]
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("path_nodes"), HeaderItem("path_relations")],
            [[path_nodes, path_relations]],
        )
    ]

    result = await repository(client).find_paths(
        GraphPathQuery(
            start_node="node-document",
            end_node="node-section",
            relationship_types=(RelationType.CONTAINS,),
        ),
        context=StorageOperationContext.system("tenant-1"),
    )

    statement, parameters = client.read_calls[0]
    assert "all(node IN nodes(path) WHERE node.tenant_id = $tenant_id" in statement
    assert parameters["relationship_types"] == ["contains"]
    assert result.paths[0].nodes[1].node_key == "node-section"


@pytest.mark.asyncio
async def test_subgraph_search_applies_bounded_filtered_traversal() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("path_nodes"), HeaderItem("path_relations")],
            [
                [
                    [node.model_dump(mode="json") for node in nodes()],
                    [relation().model_dump(mode="json")],
                ]
            ],
        )
    ]

    result = await repository(client).expand_subgraph(
        GraphSubgraphQuery(
            start_node="node-document",
            relationship_types=(RelationType.CONTAINS,),
            max_nodes=10,
        ),
        context=StorageOperationContext.system("tenant-1"),
    )

    _, parameters = client.read_calls[0]
    assert parameters["start_node"] == "node-document"
    assert parameters["relationship_types"] == ["contains"]
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
        # Separator variants must stay swallowed: a provider reword must not turn an
        # idempotent re-provision into a startup failure.
        ("already-exists", True),
        ("AlreadyExists", True),
        ("ALREADY EXISTS", True),
        # The negations must propagate: an earlier substring match on "exist" swallowed
        # these as if provisioning had succeeded.
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
