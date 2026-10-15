from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_node_resolution import (
    MAX_SELECTOR_CANDIDATES,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_queries import TRIPLET_SCAN_LIMIT
from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborDeadlineExceeded
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import (
    GraphAccessScope,
    GraphNodeResolutionQuery,
    GraphNodeSelectorKind,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_core.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem


@pytest.mark.asyncio
async def test_exact_node_resolution_preserves_ambiguity_and_parameterizes_scopes() -> None:
    client = FakeFalkorDBClient()
    records = tuple(
        GraphNodeRecord(
            node_key=f"node-{index}",
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.GITHUB_REPOSITORY,
            logical_id=f"repo-{index}",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="source-1",
            title="Payments",
        )
        for index in (1, 2)
    )
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("node")],
            [[record.model_dump(mode="json")] for record in records],
        )
    ]
    repository = FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(),
        client=client,  # type: ignore[arg-type]
    )

    result = await repository.resolve_nodes(
        GraphNodeResolutionQuery(
            selector_kind=GraphNodeSelectorKind.EXACT_TITLE,
            value="Payments",
            source_scope_ids=("source-1",),
            access_scope=GraphAccessScope(source_scope_ids=("source-1",)),
            limit=2,
        ),
        context=StorageOperationContext.system("tenant-1"),
    )

    statement, parameters = client.read_calls[0]
    assert "node.title_key = $title_key" in statement
    assert "Payments" not in statement
    assert parameters["title_key"] == "payments"
    assert parameters["source_scope_ids"] == ["source-1"]
    assert parameters["authorized_source_scope_ids"] == ["source-1"]
    assert parameters["authorized_document_ids"] == []
    assert parameters["access_unrestricted"] is False
    assert statement.index("$authorized_source_scope_ids") < statement.index("LIMIT $limit")
    assert [item.node_key for item in result.candidates] == ["node-1", "node-2"]


def _repository(client: FakeFalkorDBClient) -> FalkorKnowledgeGraphRepository:
    return FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(),
        client=client,  # type: ignore[arg-type]
    )


def _empty() -> FakeQueryResult:
    return FakeQueryResult([HeaderItem("node")], [])


def _document_version() -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key="graph-v2-document-version:1",
        node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
        entity_type=GraphEntityType.DOCUMENT_VERSION,
        logical_id="document:abc",
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        document_id="document:abc",
        document_version_id="version-1",
        source_scope_id="source-1",
        title="Release notes",
    )


@pytest.mark.asyncio
async def test_singular_resolution_tries_indexed_tiers_in_order_and_stops_at_a_hit() -> None:
    """node_key, then logical_id, then title_key: each tier is its own index-anchored
    read, and a later tier is only read when the earlier ones found nothing."""
    client = FakeFalkorDBClient()
    client.read_results = [_empty(), _empty(), _empty()]

    result = await _repository(client).expand_subgraph(
        GraphSubgraphQuery(start_node="Release Notes", max_depth=1, max_nodes=5),
        context=StorageOperationContext.system("tenant-1"),
    )

    statements = [statement for statement, _ in client.read_calls]
    assert len(statements) == 3
    assert "node.node_key = $selector" in statements[0]
    assert "node.logical_id = $selector" in statements[1]
    # The write path stores title_key as toLower(node.title); the read reuses toLower.
    assert "node.title_key = toLower($selector)" in statements[2]
    for statement in statements:
        # No tenant-wide scan and no relationship materialization before filtering.
        assert "OPTIONAL MATCH" not in statement
        assert "source_title" not in statement
        assert "toLower(node.title)" not in statement
        assert statement.index("$authorized_document_ids") < statement.index(
            "LIMIT $candidate_limit"
        )
        assert "size(candidates) <= $max_candidates" in statement
    _, parameters = client.read_calls[0]
    assert parameters["candidate_limit"] == MAX_SELECTOR_CANDIDATES + 1
    assert parameters["preferred_node_kind"] == KnowledgeNodeKind.SOURCE_ENTITY.value
    assert result.nodes == ()


@pytest.mark.asyncio
async def test_document_id_selector_resolves_through_the_logical_id_tier() -> None:
    client = FakeFalkorDBClient()
    version = _document_version()
    client.read_results = [
        _empty(),
        FakeQueryResult([HeaderItem("node")], [[version.model_dump(mode="json")]]),
        FakeQueryResult([HeaderItem("relation"), HeaderItem("related")], []),
    ]

    result = await _repository(client).expand_subgraph(
        GraphSubgraphQuery(start_node="document:abc", max_depth=1, max_nodes=5),
        context=StorageOperationContext.system("tenant-1"),
    )

    # Two resolution tiers, then the first hop; the title tier is never read.
    assert len(client.read_calls) == 3
    assert "node.logical_id = $selector" in client.read_calls[1][0]
    assert client.read_calls[1][1]["selector"] == "document:abc"
    assert [node.node_key for node in result.nodes] == [version.node_key]


@pytest.mark.asyncio
async def test_a_graph_store_timeout_surfaces_as_a_deadline_error() -> None:
    class TimingOutClient(FakeFalkorDBClient):
        async def read(self, statement: str, parameters: dict[str, Any]) -> FakeQueryResult:
            raise RuntimeError("Query timed out")

    with pytest.raises(HarborDeadlineExceeded):
        await _repository(TimingOutClient()).expand_subgraph(
            GraphSubgraphQuery(start_node="node-1", max_depth=1, max_nodes=5),
            context=StorageOperationContext.system("tenant-1"),
        )


@pytest.mark.asyncio
async def test_predicate_only_triplets_start_from_the_relationship_type_index() -> None:
    """Without a subject or object there is no node to anchor on; the read scans the
    edge index of the one type and is cut before it is ordered."""
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult([HeaderItem("subject"), HeaderItem("predicate"), HeaderItem("object")], [])
    ]

    await _repository(client).search_triplets(
        GraphTripletQuery(predicate=RelationType.HAS_ATTACHMENT, limit=5),
        context=StorageOperationContext.system("tenant-1"),
    )

    statement, parameters = client.read_calls[0]
    assert len(client.read_calls) == 1
    assert "MATCH ()-[predicate:HAS_ATTACHMENT]->()" in statement
    assert "startNode(predicate) AS subject" in statement
    assert statement.index("LIMIT $scan_limit") < statement.index("ORDER BY")
    assert parameters["scan_limit"] == TRIPLET_SCAN_LIMIT
    assert parameters["subject_key"] is None


@pytest.mark.asyncio
async def test_triplet_with_an_unresolvable_object_reads_no_relationships() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [_empty(), _empty(), _empty()]

    result = await _repository(client).search_triplets(
        GraphTripletQuery(object="missing"),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert len(client.read_calls) == 3
    assert result.triplets == ()


@pytest.mark.asyncio
async def test_path_search_between_one_node_and_itself_reads_no_paths() -> None:
    """Two different selectors (a node_key and a document_id) can name the same node;
    allShortestPaths has no non-empty path from a node to itself, so none is read."""
    client = FakeFalkorDBClient()
    version = _document_version()
    row = FakeQueryResult([HeaderItem("node")], [[version.model_dump(mode="json")]])
    client.read_results = [row, _empty(), row]

    result = await _repository(client).find_paths(
        GraphPathQuery(start_node=version.node_key, end_node="document:abc"),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert len(client.read_calls) == 3
    assert result.paths == ()
