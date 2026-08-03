from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, KnowledgeNodeKind
from harborrag_core.retrieval import GraphPath, GraphTriplet
from harborrag_mcp_server.tools.graph_search import (
    GraphPathSearchTool,
    GraphSubgraphSearchTool,
    GraphTripletSearchTool,
)
from harborrag_runtime.sdk import (
    GraphPathResponse,
    GraphSubgraphResponse,
    GraphTripletResponse,
)


def node(key: str, logical_id: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.DOCUMENT,
        logical_id=logical_id,
        document_id=logical_id,
        document_version_id=f"version-{logical_id}",
        source_scope_id="scope-1",
        title=logical_id,
    )


def edge(source: GraphNodeRecord, target: GraphNodeRecord) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id="relation-1",
        relation_type=RelationType.LINKS_TO,
        source_node_key=source.node_key,
        target_node_key=target.node_key,
        document_version_id=source.document_version_id,
        source_relation_version="source-v1",
        source_explicit=True,
    )


class StaticGraphFacade:
    def __init__(self) -> None:
        self.source = node("node-a", "document-a")
        self.target = node("node-b", "document-b")
        self.relation = edge(self.source, self.target)
        self.calls: list[object] = []

    async def search_triplets(self, request):
        self.calls.append(request)
        return GraphTripletResponse(
            triplets=(
                GraphTriplet(
                    subject=self.source,
                    predicate=self.relation,
                    object=self.target,
                ),
            ),
            diagnostics={"accepted_count": 1},
        )

    async def find_paths(self, request):
        self.calls.append(request)
        return GraphPathResponse(
            paths=(GraphPath(nodes=(self.source, self.target), relations=(self.relation,)),),
            diagnostics={"accepted_count": 1},
        )

    async def expand_subgraph(self, request):
        self.calls.append(request)
        return GraphSubgraphResponse(
            nodes=(self.source, self.target),
            relations=(self.relation,),
            diagnostics={"accepted_count": 2},
        )


def runtime():
    graph = StaticGraphFacade()
    return SimpleNamespace(graph=graph), graph


@pytest.mark.asyncio
async def test_triplet_tool_forwards_access_and_predicate() -> None:
    harbor, graph = runtime()

    result = await GraphTripletSearchTool(runtime=harbor).call(
        {"tenant_id": "demo", "subject": "document-a", "predicate": "links_to"},
        principal_id="reader-1",
    )

    assert result["ok"] is True
    request = graph.calls[0]
    assert request.access.principal_id == "reader-1"
    assert request.access.tenant_id == "demo"
    assert request.query.predicate == RelationType.LINKS_TO
    assert result["triplets"][0]["predicate"]["relation_type"] == "links_to"


@pytest.mark.asyncio
async def test_path_tool_forwards_bounds_direction_and_relationships() -> None:
    harbor, graph = runtime()

    result = await GraphPathSearchTool(runtime=harbor).call(
        {
            "tenant_id": "demo",
            "start_node": "document-a",
            "end_node": "document-b",
            "relationship_types": ["links_to"],
            "max_depth": 3,
            "max_paths": 4,
            "direction": "both",
        },
        principal_id="reader-1",
    )

    assert result["ok"] is True
    query = graph.calls[0].query
    assert query.max_depth == 3
    assert query.max_paths == 4
    assert query.direction == "both"
    assert result["paths"][0]["nodes"][0]["node_key"] == "node-a"


@pytest.mark.asyncio
async def test_subgraph_tool_returns_canonical_nodes_and_relations() -> None:
    harbor, graph = runtime()

    result = await GraphSubgraphSearchTool(runtime=harbor).call(
        {
            "tenant_id": "demo",
            "start_node": "document-a",
            "max_nodes": 2,
        },
        principal_id="reader-1",
    )

    assert result["ok"] is True
    assert len(result["nodes"]) == 2
    assert len(result["relations"]) == 1
    assert graph.calls[0].query.max_nodes == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,arguments",
    [
        (GraphTripletSearchTool(), {"tenant_id": "demo"}),
        (
            GraphTripletSearchTool(),
            {"tenant_id": "demo", "predicate": "not-a-relation"},
        ),
        (
            GraphPathSearchTool(),
            {"tenant_id": "demo", "start_node": "same", "end_node": "same"},
        ),
        (
            GraphPathSearchTool(),
            {"tenant_id": "demo", "start_node": "a", "end_node": "b", "max_depth": 9},
        ),
        (GraphSubgraphSearchTool(), {"tenant_id": "demo", "start_node": "a", "max_nodes": 21}),
    ],
)
async def test_graph_tools_reject_invalid_direct_inputs(tool, arguments) -> None:
    assert (await tool.call(arguments, principal_id="reader-1"))["ok"] is False


def test_graph_tool_schemas_are_strict_and_tenant_scoped() -> None:
    for tool in (GraphTripletSearchTool, GraphPathSearchTool, GraphSubgraphSearchTool):
        assert "tenant_id" in tool.spec.input_schema["required"]
        assert tool.spec.input_schema["additionalProperties"] is False
