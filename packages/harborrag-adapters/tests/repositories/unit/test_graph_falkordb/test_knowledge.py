from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
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
            node_kind=KnowledgeNodeKind.DOCUMENT,
            logical_id="document-1",
            document_id="document-1",
            document_version_id="version-1",
            source_scope_id="scope-1",
            title="Title with ') DETACH DELETE node //",
            connector_type=ConnectorType.LOCAL,
            document_kind=DocumentKind.LOCAL_FILE,
            source_item_id="docs/release.md",
            source_uri="file:///docs/release.md",
            content_preview="The release timeout is 30 seconds.",
        ),
        GraphNodeRecord(
            node_key="node-section",
            node_kind=KnowledgeNodeKind.SECTION,
            logical_id="section-1",
            document_id="document-1",
            document_version_id="version-1",
            source_scope_id="scope-1",
            title="Operations",
        ),
    )


def relation() -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id="relation-1",
        relation_type=RelationType.HAS_SECTION,
        source_node_key="node-document",
        target_node_key="node-section",
        document_version_id="version-1",
        source_relation_version="graph-v1",
        source_explicit=False,
        evidence_chunk_ids=("chunk-1",),
    )


@pytest.mark.asyncio
async def test_provision_creates_exact_indexes_and_unique_node_constraint() -> None:
    client = FakeFalkorDBClient()

    await repository(client).connect()

    assert client.connected is True
    assert len(client.write_calls) == 4
    assert all("CREATE INDEX" in statement for statement, _ in client.write_calls)
    assert client.constraint_calls == [("KnowledgeNode", ("node_key",))]


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
        row["content_preview"] == "The release timeout is 30 seconds."
        and row["source_uri"] == "file:///docs/release.md"
        and row["connector_type"] == "local"
        for _, parameters in client.write_calls
        for row in parameters.get("rows", ())
    )


@pytest.mark.asyncio
async def test_verification_checks_endpoints_evidence_and_duplicate_identities() -> None:
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
                HeaderItem("evidence_chunk_ids"),
                HeaderItem("occurrences"),
            ],
            [["relation-1", "node-document", "node-section", ["chunk-1"], 1]],
        ),
    ]

    result = await repository(client).verify_projection(
        nodes(),
        (relation(),),
        available_chunk_ids=("chunk-1",),
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
                HeaderItem("evidence_chunk_ids"),
                HeaderItem("occurrences"),
            ],
            [],
        ),
    ]

    result = await repository(client).verify_projection(
        nodes(),
        (relation(),),
        available_chunk_ids=("chunk-1",),
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
            relationship_types=(RelationType.HAS_SECTION,),
        ),
        context=StorageOperationContext.system("tenant-1"),
    )

    statement, parameters = client.read_calls[0]
    assert "all(node IN nodes(path) WHERE node.tenant_id = $tenant_id)" in statement
    assert parameters["relationship_types"] == ["has_section"]
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
            relationship_types=(RelationType.HAS_SECTION,),
            max_nodes=10,
        ),
        context=StorageOperationContext.system("tenant-1"),
    )

    _, parameters = client.read_calls[0]
    assert parameters["start_node"] == "node-document"
    assert parameters["relationship_types"] == ["has_section"]
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
