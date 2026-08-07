from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import GraphPath, GraphTriplet
from harborrag_mcp_server.tools.graph_search import (
    GraphNeighborhoodTool,
    GraphPathSearchTool,
    GraphSubgraphSearchTool,
    GraphTripletSearchTool,
)
from harborrag_runtime.sdk import (
    GraphNeighborhoodResponse,
    GraphPathResponse,
    GraphSubgraphResponse,
    GraphTripletResponse,
)


def node(key: str, logical_id: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
        logical_id=logical_id,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        title=logical_id,
    )


def edge(source: GraphNodeRecord, target: GraphNodeRecord) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id="relation-1",
        relation_type=RelationType.LINKS_TO,
        source_node_key=source.node_key,
        target_node_key=target.node_key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
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

    async def neighborhood(self, request):
        self.calls.append(request)
        return GraphNeighborhoodResponse(
            seeds=("chunk:seed-1",),
            nodes=(self.source, self.target),
            relations=(self.relation,),
            diagnostics={"accepted_count": 2},
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


class _RaisingGraphFacade:
    async def search_triplets(self, request):
        raise RuntimeError("graph store unreachable")

    async def find_paths(self, request):
        raise RuntimeError("graph store unreachable")

    async def neighborhood(self, request):
        raise RuntimeError("graph store unreachable")

    async def expand_subgraph(self, request):
        raise RuntimeError("graph store unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_cls,arguments,log_message",
    [
        (
            GraphNeighborhoodTool,
            {"tenant_id": "demo", "query": "release owner"},
            "graph_neighborhood backend raised during call",
        ),
        (
            GraphTripletSearchTool,
            {"tenant_id": "demo", "subject": "document-a"},
            "graph_triplet_search backend raised during call",
        ),
        (
            GraphPathSearchTool,
            {"tenant_id": "demo", "start_node": "a", "end_node": "b"},
            "graph_path_search backend raised during call",
        ),
        (
            GraphSubgraphSearchTool,
            {"tenant_id": "demo", "start_node": "a"},
            "graph_subgraph_search backend raised during call",
        ),
    ],
)
async def test_graph_tool_backend_failure_returns_generic_error_but_logs_the_cause(
    tool_cls, arguments, log_message, caplog
) -> None:
    harbor = SimpleNamespace(graph=_RaisingGraphFacade())

    with caplog.at_level("ERROR", logger="harborrag.mcp.tools.graph_search"):
        result = await tool_cls(runtime=harbor).call(arguments, principal_id="reader-1")

    assert result == {"ok": False, "error": "graph retrieval backend failed"}
    logged = [record for record in caplog.records if record.exc_info is not None]
    assert logged, "the real exception must be logged even though the caller sees a generic error"
    assert logged[0].message == log_message
    assert "graph store unreachable" in str(logged[0].exc_info[1])


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
    assert result["triplets"][0]["predicate"] == "links_to"
    assert result["triplets"][0]["subject"]["node_key"] == "node-a"
    # Write-side bookkeeping must not reach an LLM caller.
    assert "owner_id" not in result["triplets"][0]["subject"]
    assert "attributes" not in result["triplets"][0]["subject"]


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
        (GraphNeighborhoodTool(), {"tenant_id": "demo"}),
        (GraphNeighborhoodTool(), {"tenant_id": "demo", "query": "q", "seed_limit": 11}),
    ],
)
async def test_graph_tools_reject_invalid_direct_inputs(tool, arguments) -> None:
    assert (await tool.call(arguments, principal_id="reader-1"))["ok"] is False


def test_graph_tool_schemas_are_strict_and_tenant_scoped() -> None:
    for tool in (
        GraphTripletSearchTool,
        GraphPathSearchTool,
        GraphSubgraphSearchTool,
        GraphNeighborhoodTool,
    ):
        assert "tenant_id" in tool.spec.input_schema["required"]
        assert tool.spec.input_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_neighborhood_tool_needs_no_node_selector() -> None:
    harbor, graph = runtime()

    result = await GraphNeighborhoodTool(runtime=harbor).call(
        {"tenant_id": "demo", "query": "how do we release?"},
        principal_id="reader-1",
    )

    assert result["ok"] is True
    # The point of this tool: a free-text question is the only required input.
    assert graph.calls[0].query.query == "how do we release?"
    assert result["seeds"] == ["chunk:seed-1"]
    assert [item["node_key"] for item in result["nodes"]] == ["node-a", "node-b"]


def test_graph_tool_schemas_only_offer_projected_predicates() -> None:
    schema = GraphTripletSearchTool.spec.input_schema
    predicates = schema["properties"]["predicate"]["enum"]

    assert "links_to" in predicates
    # Reserved members are never emitted by the projection, so filtering on one can only
    # ever return an empty result the caller would misread as a genuine miss.
    for reserved in ("mentions", "child_of", "attached_to", "has_section"):
        assert reserved not in predicates


def test_path_search_defaults_to_an_undirected_walk() -> None:
    schema = GraphPathSearchTool.spec.input_schema

    # (:Chunk)-[:SUPPORTS]->(:Structure) points into the spine while CONTAINS points down
    # it, so an outgoing-only default cannot reach a chunk's own document.
    assert schema["properties"]["direction"]["default"] == "both"


def test_mcp_and_agent_tool_schemas_are_the_same_definition() -> None:
    from harborrag_runtime.agent.tool_specs import RUNTIME_AGENT_TOOL_SPECS

    agent_specs = {spec.name: spec for spec in RUNTIME_AGENT_TOOL_SPECS}
    for tool in (
        GraphTripletSearchTool,
        GraphPathSearchTool,
        GraphSubgraphSearchTool,
        GraphNeighborhoodTool,
    ):
        agent = agent_specs[tool.spec.name]
        assert tool.spec.description == agent.description
        assert tool.spec.input_schema["required"] == agent.input_schema["required"]
        assert set(tool.spec.input_schema["properties"]) == set(agent.input_schema["properties"])
