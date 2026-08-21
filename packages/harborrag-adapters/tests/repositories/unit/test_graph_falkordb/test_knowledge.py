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
        shared = tuple(node.model_copy(update={"owner_id": tenant_id}) for node in nodes())
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
    # FalkorDB rejects ORDER BY over an unprojected path expression, so the ordering has
    # to name the projected alias. Pinned because the failure is query-time only.
    assert "ORDER BY size(path_relations)" in statement
    assert parameters["relationship_types"] == ["contains"]
    assert result.paths[0].nodes[1].node_key == "node-section"


@pytest.mark.asyncio
async def test_expand_subgraph_walks_one_hop_at_a_time_instead_of_variable_length() -> None:
    """A single MATCH ...-[*1..max_depth]-... forces the engine to enumerate nearly every
    walk up to max_depth before it can sort and LIMIT, which times out on a densely
    connected tenant long before max_depth reaches its upper bound. Expanding one hop per
    round trip bounds every query to a single-hop pattern from an already-known frontier,
    so cost scales with max_nodes rather than with max_depth."""
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
        GraphSubgraphQuery(start_node="node-document", max_depth=1, max_nodes=10),
        context=StorageOperationContext.system("tenant-1"),
    )

    # start resolution, then one round trip for the single level max_depth allows.
    assert len(client.read_calls) == 2
    for statement, _ in client.read_calls:
        assert "*1.." not in statement
    assert {node.node_key for node in result.nodes} == {"node-document", "node-section"}
    assert len(result.relations) == 1
    assert result.truncated is False


@pytest.mark.asyncio
async def test_expand_subgraph_stops_once_max_nodes_is_reached() -> None:
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
        GraphSubgraphQuery(start_node="node-document", max_depth=5, max_nodes=2),
        context=StorageOperationContext.system("tenant-1"),
    )

    # max_nodes is reached after the first level, so no further levels are queried even
    # though max_depth=5 would otherwise allow up to 4 more round trips.
    assert len(client.read_calls) == 2
    assert len(result.nodes) == 2
    assert result.truncated is True
