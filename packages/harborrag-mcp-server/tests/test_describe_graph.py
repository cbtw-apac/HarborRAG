from __future__ import annotations

import pytest
from jsonschema.validators import validator_for

from harborrag_core.chunking import PROJECTED_RELATION_TYPES
from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.ingestion.projection_contracts import GRAPH_SCHEMA_VERSION
from harborrag_mcp_server.tools.describe_graph import DescribeGraphTool
from harborrag_mcp_server.tools.graph_catalog import (
    ONTOLOGY_SCHEMA_VERSION,
    SEMANTIC_SCHEMA_VERSION,
)


@pytest.mark.asyncio
async def test_describe_graph_accepts_an_empty_object_and_needs_no_runtime() -> None:
    tool = DescribeGraphTool()
    assert tool.spec.input_schema["additionalProperties"] is False
    assert set(tool.spec.input_schema["properties"]) == set()

    result = await tool.call({}, principal_id="in-process")

    assert result["ok"] is True
    assert result["versions"]["structural"] == GRAPH_SCHEMA_VERSION


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


@pytest.mark.asyncio
async def test_describe_graph_versions_cover_structural_semantic_and_ontology() -> None:
    result = await DescribeGraphTool().call({}, principal_id="in-process")

    assert result["versions"] == {
        "structural": GRAPH_SCHEMA_VERSION,
        "semantic": SEMANTIC_SCHEMA_VERSION,
        "ontology": ONTOLOGY_SCHEMA_VERSION,
    }


@pytest.mark.asyncio
async def test_describe_graph_layers_come_from_canonical_enums() -> None:
    result = await DescribeGraphTool().call({}, principal_id="in-process")

    assert result["layers"]["nodes"] == [kind.value for kind in KnowledgeNodeKind]
    assert result["layers"]["relations"] == [
        relation.value for relation in PROJECTED_RELATION_TYPES
    ]


@pytest.mark.asyncio
async def test_describe_graph_properties_cover_common_and_named_property_sets() -> None:
    result = await DescribeGraphTool().call({}, principal_id="in-process")

    assert set(result["properties"]) == {
        "common_node",
        "document_owned",
        "Chunk",
        "Entity",
        "RELATES",
    }
    for property_names in result["properties"].values():
        assert property_names
        assert all(isinstance(name, str) and name for name in property_names)


@pytest.mark.asyncio
async def test_describe_graph_never_accepts_extra_arguments() -> None:
    from harborrag_mcp_server.server.server import McpServer

    server = McpServer()
    with pytest.raises(ValueError, match="do not match"):
        await server.call_tool("describe_graph", {"query": "anything"})


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
