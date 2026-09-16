from __future__ import annotations

import pytest
from jsonschema.validators import validator_for

from harborrag_core.chunking import (
    CHUNK_PROPERTIES,
    COMMON_NODE_PROPERTIES,
    DOCUMENT_OWNED_PROPERTIES,
    ENTITY_PROPERTIES,
    PROJECTED_RELATION_TYPES,
    RELATES_PROPERTIES,
)
from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.ingestion.projection_contracts import (
    GRAPH_SCHEMA_VERSION,
    ONTOLOGY_SCHEMA_VERSION,
    SEMANTIC_SCHEMA_VERSION,
)
from harborrag_runtime.tools.describe_graph import DescribeGraphTool
from harborrag_runtime.tools.describe_graph import DescribeGraphTool as RuntimeDescribeGraphTool


@pytest.mark.asyncio
async def test_describe_graph_accepts_an_empty_object_and_needs_no_runtime() -> None:
    tool = DescribeGraphTool()
    assert tool.spec.input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    result = await tool.call({}, principal_id="in-process")

    assert set(result) == {"ok", "versions", "layers", "properties"}
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_describe_graph_output_matches_its_advertised_schema() -> None:
    tool = DescribeGraphTool()
    result = await tool.call({}, principal_id="in-process")

    validator_type = validator_for(tool.spec.output_schema)
    validator_type.check_schema(tool.spec.output_schema)
    validator_type(tool.spec.output_schema).validate(result)


def test_describe_graph_advertises_read_only_annotations() -> None:
    assert DescribeGraphTool.spec.annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_describe_graph_is_shared_with_the_runtime() -> None:
    assert DescribeGraphTool is RuntimeDescribeGraphTool


@pytest.mark.asyncio
async def test_describe_graph_versions_come_from_current_core_contracts() -> None:
    result = await DescribeGraphTool().call({}, principal_id="in-process")

    assert result["versions"] == {
        "structural": GRAPH_SCHEMA_VERSION,
        "semantic": SEMANTIC_SCHEMA_VERSION,
        "ontology": ONTOLOGY_SCHEMA_VERSION,
    }


@pytest.mark.asyncio
async def test_describe_graph_layers_come_from_canonical_enums() -> None:
    result = await DescribeGraphTool().call({}, principal_id="in-process")

    assert result["layers"] == {
        "nodes": [kind.value for kind in KnowledgeNodeKind],
        "relations": [relation.value for relation in PROJECTED_RELATION_TYPES],
    }


@pytest.mark.asyncio
async def test_describe_graph_properties_come_from_current_core_contracts() -> None:
    result = await DescribeGraphTool().call({}, principal_id="in-process")

    assert result["properties"] == {
        "common_node": list(COMMON_NODE_PROPERTIES),
        "document_owned": list(DOCUMENT_OWNED_PROPERTIES),
        "Chunk": list(CHUNK_PROPERTIES),
        "Entity": list(ENTITY_PROPERTIES),
        "RELATES": list(RELATES_PROPERTIES),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "removed_key",
    [
        "graph_schema_version",
        "capabilities",
        "selector_rules",
        "node_kinds",
        "entity_types",
        "relations",
        "meanings",
        "topologies",
        "workflows",
        "defaults",
        "argument_constraints",
        "limits",
    ],
)
async def test_describe_graph_schema_rejects_removed_sections(removed_key: str) -> None:
    tool = DescribeGraphTool()
    result = await tool.call({}, principal_id="in-process")
    validator = validator_for(tool.spec.output_schema)(tool.spec.output_schema)

    assert not validator.is_valid({**result, removed_key: {}})


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["versions", "layers", "properties"])
async def test_describe_graph_schema_rejects_unknown_nested_keys(section: str) -> None:
    tool = DescribeGraphTool()
    result = await tool.call({}, principal_id="in-process")
    result[section]["unknown"] = []
    validator = validator_for(tool.spec.output_schema)(tool.spec.output_schema)

    assert not validator.is_valid(result)


@pytest.mark.asyncio
async def test_describe_graph_payload_mutations_do_not_affect_later_calls() -> None:
    tool = DescribeGraphTool()
    original = await tool.call({}, principal_id="in-process")
    modified = await tool.call({}, principal_id="in-process")
    modified["versions"]["structural"] = "modified"
    modified["layers"]["nodes"].append("Modified")
    modified["properties"]["Chunk"].append("modified")

    assert await tool.call({}, principal_id="in-process") == original


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"query": "anything"}, {"for_tool": "vector_search"}])
async def test_describe_graph_never_accepts_extra_arguments(arguments) -> None:
    from harborrag_mcp_server.server.server import McpServer

    server = McpServer()
    with pytest.raises(ValueError, match="do not match"):
        await server.call_tool("describe_graph", arguments)


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
    server = McpServer(tools=[DescribeGraphTool()])
    server.configuration = McpConfigurationStore.load(
        path=config_path,
        specs=server.list_tools(),
        audit=McpAuditLog(),
    )

    assert "describe_graph" not in [tool.name for tool in server.list_tools()]
    with pytest.raises(PermissionError, match="disabled"):
        await server.call_tool("describe_graph")
