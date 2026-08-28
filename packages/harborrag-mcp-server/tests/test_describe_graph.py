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
