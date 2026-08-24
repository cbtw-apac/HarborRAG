from __future__ import annotations

import pytest

from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import GraphSubgraphQuery
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem
from .test_knowledge import nodes, relation, repository


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


def _colliding_title_nodes() -> tuple[GraphNodeRecord, GraphNodeRecord]:
    """Two distinct nodes that collide on toLower(title), the resolution branch a
    start_node value falls back to when it matches neither node_key nor logical_id."""
    base = {
        "node_kind": KnowledgeNodeKind.STRUCTURE,
        "entity_type": GraphEntityType.SECTION,
        "ownership_scope": GraphOwnershipScope.DOCUMENT_VERSION,
        "owner_id": "tenant-1",
        "document_id": "document-1",
        "document_version_id": "version-1",
        "source_scope_id": "scope-1",
    }
    return (
        GraphNodeRecord(node_key="node-ops-a", logical_id="section-a", title="Ops", **base),
        GraphNodeRecord(node_key="node-ops-b", logical_id="section-b", title="OPS", **base),
    )


@pytest.mark.asyncio
async def test_expand_subgraph_resolves_start_node_to_a_single_row() -> None:
    """The start-resolution query must constrain the OR'd node_key/logical_id/title match
    to exactly one row via ORDER BY + LIMIT 1, not LIMIT $max_nodes: max_nodes bounds the
    traversal frontier, not how many candidate start nodes get seeded into it."""
    client = FakeFalkorDBClient()
    node_a, _node_b = _colliding_title_nodes()
    client.read_results = [
        FakeQueryResult([HeaderItem("node")], [[node_a.model_dump(mode="json")]]),
        FakeQueryResult([HeaderItem("relation"), HeaderItem("related")], []),
    ]

    result = await repository(client).expand_subgraph(
        GraphSubgraphQuery(start_node="Ops", max_depth=1, max_nodes=10),
        context=StorageOperationContext.system("tenant-1"),
    )

    start_statement, start_parameters = client.read_calls[0]
    assert "ORDER BY start.node_key" in start_statement
    assert "LIMIT 1" in start_statement
    assert "max_nodes" not in start_parameters
    assert {node.node_key for node in result.nodes} == {"node-ops-a"}


@pytest.mark.asyncio
async def test_expand_subgraph_rejects_multiple_start_row_matches() -> None:
    """If the start-resolution query ever returns more than one row (e.g. the ORDER BY +
    LIMIT 1 constraint above regresses), expand_subgraph must fail loudly instead of
    silently seeding the BFS frontier with every colliding node."""
    client = FakeFalkorDBClient()
    node_a, node_b = _colliding_title_nodes()
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("node")],
            [[node_a.model_dump(mode="json")], [node_b.model_dump(mode="json")]],
        ),
    ]

    with pytest.raises(AssertionError):
        await repository(client).expand_subgraph(
            GraphSubgraphQuery(start_node="Ops", max_depth=1, max_nodes=10),
            context=StorageOperationContext.system("tenant-1"),
        )
