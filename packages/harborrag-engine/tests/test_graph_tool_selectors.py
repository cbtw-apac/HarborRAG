"""Graph tools accept the ids search hands out and explain store timeouts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.contracts.errors import HarborDeadlineExceeded
from harborrag_core.contracts.reader import (
    GraphNodeResolveResponse,
    GraphPathResponse,
    GraphSubgraphResponse,
    GraphTripletResponse,
)
from harborrag_core.retrieval import GraphNodeSelectorKind
from harborrag_engine.tools.graph_search import (
    GraphPathSearchTool,
    GraphSubgraphSearchTool,
    GraphTripletSearchTool,
)
from harborrag_engine.tools.graph_search_support import node_selector
from harborrag_engine.tools.reader_tools import ResolveGraphNodesTool

_DIAGNOSTICS: dict[str, object] = {
    "candidate_count": 0,
    "accepted_count": 0,
    "stale_count": 0,
    "unpublished_count": 0,
    "projection_truncated": False,
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # vector_search metadata.source_item_id -> the provider id the node is keyed by.
        ("jira://CPM/CPM-110455", "CPM-110455"),
        ("jira://CPM/CPM-66246/attachments/256636", "256636"),
        ("confluence://SPACE/91980256", "91980256"),
        ("github://owner/repo/src/app.py", "src/app.py"),
        # Everything else is already a selector and passes unchanged.
        ("CPM-110455", "CPM-110455"),
        ("chunk:9435", "chunk:9435"),
        ("document:5d58", "document:5d58"),
        ("graph-v2-source-entity:b64a", "graph-v2-source-entity:b64a"),
        ("Handover Document", "Handover Document"),
        ("https://example.com/page", "https://example.com/page"),
    ],
)
def test_node_selector_reduces_source_item_ids_like_the_projection(
    value: str, expected: str
) -> None:
    assert node_selector(value) == expected


class _RecordingGraph:
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[object] = []
        self.error = error

    async def find_paths(self, request: object) -> GraphPathResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return GraphPathResponse(paths=(), diagnostics=dict(_DIAGNOSTICS))

    async def search_triplets(self, request: object) -> GraphTripletResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return GraphTripletResponse(triplets=(), diagnostics=dict(_DIAGNOSTICS))

    async def expand_subgraph(self, request: object) -> GraphSubgraphResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return GraphSubgraphResponse(nodes=(), relations=(), diagnostics=dict(_DIAGNOSTICS))


@pytest.mark.asyncio
async def test_path_and_subgraph_tools_map_source_item_ids_to_provider_ids() -> None:
    graph = _RecordingGraph()
    runtime = SimpleNamespace(graph=graph)

    await GraphPathSearchTool(runtime=runtime).call(
        {
            "tenant_id": "demo",
            "start_node": "jira://CPM/CPM-110455",
            "end_node": "jira://CPM/CPM-110455/attachments/365004",
        },
        principal_id="reader-1",
    )
    await GraphSubgraphSearchTool(runtime=runtime).call(
        {"tenant_id": "demo", "start_node": "jira://CPM/CPM-110455"},
        principal_id="reader-1",
    )

    await GraphTripletSearchTool(runtime=runtime).call(
        {"tenant_id": "demo", "object": "jira://CPM/CPM-66246/attachments/256636"},
        principal_id="reader-1",
    )

    path_request, subgraph_request, triplet_request = graph.requests
    assert path_request.query.start_node == "CPM-110455"  # type: ignore[attr-defined]
    assert path_request.query.end_node == "365004"  # type: ignore[attr-defined]
    assert subgraph_request.query.start_node == "CPM-110455"  # type: ignore[attr-defined]
    assert triplet_request.query.subject is None  # type: ignore[attr-defined]
    assert triplet_request.query.object == "256636"  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "arguments", "narrow"),
    [
        (
            GraphPathSearchTool,
            {"tenant_id": "demo", "start_node": "a", "end_node": "b"},
            "max_depth, max_paths or relationship_types",
        ),
        (
            GraphSubgraphSearchTool,
            {"tenant_id": "demo", "start_node": "a"},
            "max_depth, max_nodes or relationship_types",
        ),
        (
            GraphTripletSearchTool,
            {"tenant_id": "demo", "predicate": "contains"},
            "by adding a subject or object, or lower limit",
        ),
    ],
)
async def test_a_graph_timeout_is_reported_as_actionable(
    tool_cls: type[GraphPathSearchTool | GraphSubgraphSearchTool | GraphTripletSearchTool],
    arguments: dict[str, object],
    narrow: str,
) -> None:
    runtime = SimpleNamespace(
        graph=_RecordingGraph(HarborDeadlineExceeded("graph query timed out"))
    )

    result = await tool_cls(runtime=runtime).call(arguments, principal_id="reader-1")

    assert result == {"ok": False, "error": f"graph query timed out; narrow {narrow}"}


class _RecordingKnowledge:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def resolve_graph_nodes(self, request: object) -> GraphNodeResolveResponse:
        self.requests.append(request)
        return GraphNodeResolveResponse("nodes-1", ())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        (GraphNodeSelectorKind.PROVIDER_ID, "jira://CPM/CPM-110455", "CPM-110455"),
        (GraphNodeSelectorKind.PROVIDER_ID, "CPM-110455", "CPM-110455"),
        # Only provider ids are reduced; a title or node key is matched as given.
        (GraphNodeSelectorKind.EXACT_TITLE, "jira://CPM/CPM-110455", "jira://CPM/CPM-110455"),
    ],
)
async def test_resolve_graph_nodes_accepts_source_item_ids_as_provider_ids(
    kind: GraphNodeSelectorKind, value: str, expected: str
) -> None:
    knowledge = _RecordingKnowledge()

    await ResolveGraphNodesTool(knowledge=knowledge).call(  # type: ignore[arg-type]
        {"tenant_id": "demo", "selector": {"kind": kind.value, "value": value}},
        principal_id="reader-1",
    )

    assert knowledge.requests[0].query.value == expected  # type: ignore[attr-defined]
