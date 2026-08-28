from __future__ import annotations

import pytest
from jsonschema.validators import validator_for

from harborrag_core.chunking import PROJECTED_RELATION_TYPES
from harborrag_core.ingestion import GraphEntityType, KnowledgeNodeKind
from harborrag_core.ingestion.projection_contracts import GRAPH_SCHEMA_VERSION
from harborrag_mcp_server.tools.describe_graph import DescribeGraphTool
from harborrag_mcp_server.tools.graph_catalog import (
    CONNECTOR_TOPOLOGIES,
    missing_entity_type_docs,
    missing_node_kind_docs,
    missing_projected_relation_docs,
)


@pytest.mark.asyncio
async def test_describe_graph_accepts_an_empty_object_and_needs_no_runtime() -> None:
    tool = DescribeGraphTool()
    assert tool.spec.input_schema == {"type": "object", "additionalProperties": False}

    result = await tool.call({}, principal_id="in-process")

    assert result["ok"] is True
    assert result["graph_schema_version"] == GRAPH_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_describe_graph_output_matches_its_advertised_schema() -> None:
    tool = DescribeGraphTool()
    result = await tool.call({}, principal_id="in-process")

    validator_type = validator_for(tool.spec.output_schema)
    validator_type.check_schema(tool.spec.output_schema)
    validator_type(tool.spec.output_schema).validate(result)


def test_describe_graph_advertises_read_only_annotations() -> None:
    annotations = DescribeGraphTool().spec.annotations
    assert annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_enum_backed_documentation_is_complete() -> None:
    assert missing_node_kind_docs() == []
    assert missing_entity_type_docs() == []
    assert missing_projected_relation_docs() == []


def test_reserved_non_projected_relations_are_absent() -> None:
    from harborrag_mcp_server.tools.graph_catalog import RELATION_MEANINGS

    assert set(RELATION_MEANINGS) <= set(PROJECTED_RELATION_TYPES)


def test_connector_topologies_cover_every_documented_provider() -> None:
    connectors = {topology["connector"] for topology in CONNECTOR_TOPOLOGIES}
    assert connectors == {"confluence", "jira", "github", "sharepoint", "local"}
    for topology in CONNECTOR_TOPOLOGIES:
        assert len(topology["entity_chain"]) >= 2


@pytest.mark.asyncio
async def test_describe_graph_node_kinds_and_entity_types_come_from_canonical_enums() -> None:
    result = await DescribeGraphTool().call({}, principal_id="in-process")

    assert {item["name"] for item in result["node_kinds"]} == {
        kind.value for kind in KnowledgeNodeKind
    }
    assert {item["name"] for item in result["entity_types"]} == {
        entity.value for entity in GraphEntityType
    }


@pytest.mark.asyncio
async def test_describe_graph_defaults_are_read_from_the_live_tool_schemas() -> None:
    from harborrag_mcp_server.tools.graph_search import (
        GraphPathSearchTool,
        GraphSubgraphSearchTool,
        GraphTripletSearchTool,
    )
    from harborrag_mcp_server.tools.vector_search import VectorSearchTool

    result = await DescribeGraphTool().call({}, principal_id="in-process")
    defaults = result["defaults"]

    assert defaults.keys() == {
        "vector_search",
        "graph_triplet_search",
        "graph_path_search",
        "graph_subgraph_search",
    }
    for tool_name, spec in (
        ("vector_search", VectorSearchTool.spec),
        ("graph_triplet_search", GraphTripletSearchTool.spec),
        ("graph_path_search", GraphPathSearchTool.spec),
        ("graph_subgraph_search", GraphSubgraphSearchTool.spec),
    ):
        for property_name, property_schema in spec.input_schema["properties"].items():
            if isinstance(property_schema, dict) and "default" in property_schema:
                assert defaults[tool_name][property_name] == property_schema["default"]


def test_maximum_depth_and_results_are_derived_not_restated() -> None:
    from harborrag_mcp_server.policy import McpToolPolicy
    from harborrag_mcp_server.tools.graph_catalog import MAXIMUM_DEPTH, MAXIMUM_RESULTS
    from harborrag_mcp_server.tools.graph_search import (
        GraphPathSearchTool,
        GraphSubgraphSearchTool,
    )

    path_properties = GraphPathSearchTool.spec.input_schema["properties"]
    subgraph_properties = GraphSubgraphSearchTool.spec.input_schema["properties"]
    assert MAXIMUM_DEPTH == path_properties["max_depth"]["maximum"]
    assert MAXIMUM_DEPTH == subgraph_properties["max_depth"]["maximum"]
    assert MAXIMUM_RESULTS == McpToolPolicy().max_results


@pytest.mark.asyncio
async def test_describe_graph_can_be_disabled_through_configuration(tmp_path) -> None:
    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.configuration import McpConfigurationStore
    from harborrag_mcp_server.server.server import McpServer

    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        "version: 1\ntools:\n  describe_graph:\n    enabled: false\n",
        encoding="utf-8",
    )
    server = McpServer()
    server.configuration = McpConfigurationStore.load(
        path=config_path,
        specs=server.list_tools(),
        audit=McpAuditLog(),
    )

    assert "describe_graph" not in [tool.name for tool in server.list_tools()]
    with pytest.raises(PermissionError, match="disabled"):
        await server.call_tool("describe_graph")
