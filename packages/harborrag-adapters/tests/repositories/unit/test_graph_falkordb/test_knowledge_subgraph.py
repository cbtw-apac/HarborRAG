from __future__ import annotations

import pytest

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
