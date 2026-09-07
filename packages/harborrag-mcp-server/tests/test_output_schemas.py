"""Prove the shared output schema fragments actually reject violations.

A schema that only validates top-level keys and container types (``{"type": "array"}``
with no ``items``) accepts anything inside. These tests construct a minimal valid
instance for each fragment, then mutate it one violation at a time -- an unexpected
extra key, a missing required key, an out-of-enum value -- and assert the validator
now rejects it. Passing the valid instance alone would not prove the schema is strict;
a loose schema passes valid instances too.
"""

from __future__ import annotations

import pytest
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from harborrag_core.invariants import HarborInvariantError
from harborrag_mcp_server.server.server import McpServer
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp_server.tools.describe_graph_schema import OUTPUT_SCHEMA
from harborrag_mcp_server.tools.output_schemas import (
    GRAPH_SEARCH_DIAGNOSTICS_SCHEMA,
    NODE_SCHEMA,
    PATH_SCHEMA,
    RELATION_SCHEMA,
    RETRIEVAL_DIAGNOSTICS_SCHEMA,
    RETRIEVAL_RESULT_SCHEMA,
    TRIPLET_SCHEMA,
)


def _validator(schema: dict[str, object]):
    validator_type = validator_for(schema)
    validator_type.check_schema(schema)
    return validator_type(schema)


VALID_NODE = {"node_key": "chunk:1", "node_kind": "Chunk", "entity_type": "chunk"}
VALID_RELATION = {
    "relation_type": "supports",
    "source_node_key": "chunk:1",
    "target_node_key": "structure:1",
}


@pytest.mark.parametrize(
    "instance",
    [
        {**VALID_NODE, "unexpected": "field"},
        {**VALID_NODE, "node_kind": "NotARealKind"},
        {k: v for k, v in VALID_NODE.items() if k != "entity_type"},
    ],
)
def test_node_schema_rejects_violations(instance) -> None:
    with pytest.raises(ValidationError):
        _validator(NODE_SCHEMA).validate(instance)


def test_node_schema_accepts_the_valid_shape() -> None:
    _validator(NODE_SCHEMA).validate(VALID_NODE)
    _validator(NODE_SCHEMA).validate({**VALID_NODE, "title": "Some Title"})


@pytest.mark.parametrize(
    "instance",
    [
        {**VALID_RELATION, "unexpected": "field"},
        {**VALID_RELATION, "relation_type": "mentions"},  # reserved, never projected
        {k: v for k, v in VALID_RELATION.items() if k != "target_node_key"},
    ],
)
def test_relation_schema_rejects_violations(instance) -> None:
    with pytest.raises(ValidationError):
        _validator(RELATION_SCHEMA).validate(instance)


@pytest.mark.parametrize(
    "instance",
    [
        {"subject": VALID_NODE, "predicate": "supports", "object": VALID_NODE, "extra": 1},
        {
            "subject": {**VALID_NODE, "unexpected": "x"},
            "predicate": "supports",
            "object": VALID_NODE,
        },
        {"subject": VALID_NODE, "predicate": "supports"},
    ],
)
def test_triplet_schema_rejects_violations(instance) -> None:
    with pytest.raises(ValidationError):
        _validator(TRIPLET_SCHEMA).validate(instance)


def test_path_schema_rejects_too_few_nodes_or_relations() -> None:
    with pytest.raises(ValidationError):
        _validator(PATH_SCHEMA).validate({"nodes": [VALID_NODE], "relations": []})


def test_graph_search_diagnostics_schema_rejects_a_missing_or_extra_field() -> None:
    valid = {
        "candidate_count": 1,
        "accepted_count": 1,
        "stale_count": 0,
        "unpublished_count": 0,
        "projection_truncated": False,
    }
    _validator(GRAPH_SEARCH_DIAGNOSTICS_SCHEMA).validate(valid)
    with pytest.raises(ValidationError):
        _validator(GRAPH_SEARCH_DIAGNOSTICS_SCHEMA).validate({**valid, "extra_count": 1})
    with pytest.raises(ValidationError):
        _validator(GRAPH_SEARCH_DIAGNOSTICS_SCHEMA).validate(
            {k: v for k, v in valid.items() if k != "stale_count"}
        )


def test_retrieval_result_schema_rejects_an_unlisted_metadata_key() -> None:
    valid = {
        "id": "chunk:1",
        "text": "content",
        "score": 0.9,
        "metadata": {
            "document_id": "doc-1",
            "document_version_id": "version-1",
            "record_kind": "evidence",
            "chunk_kind": "text",
            "connector_type": "local",
            "citation_locator": {},
            "quality_score": None,
            "retrieval_source": "qdrant-authoritative",
        },
    }
    _validator(RETRIEVAL_RESULT_SCHEMA).validate(valid)
    invalid = {**valid, "metadata": {**valid["metadata"], "unlisted": "value"}}
    with pytest.raises(ValidationError):
        _validator(RETRIEVAL_RESULT_SCHEMA).validate(invalid)


def test_retrieval_diagnostics_schema_rejects_a_malformed_graph_document() -> None:
    valid = {
        "candidate_hits": 1,
        "stale_candidates": 0,
        "unpublished_candidates": 0,
        "malformed_candidates": 0,
        "search_window": 1,
        "graph_nodes": 0,
        "graph_relations": 0,
        "graph_truncated": False,
        "duration_ms": 1.0,
        "graph_documents": [],
    }
    _validator(RETRIEVAL_DIAGNOSTICS_SCHEMA).validate(valid)
    malformed = {
        **valid,
        "graph_documents": [{"document_id": "doc-1"}],  # missing title/sections/related_results
    }
    with pytest.raises(ValidationError):
        _validator(RETRIEVAL_DIAGNOSTICS_SCHEMA).validate(malformed)


def test_describe_graph_output_schema_rejects_an_unknown_entity_type_or_extra_key() -> None:
    valid_entry = {"name": "chunk", "meaning": "Citation-ready indexed evidence."}
    entity_types_schema = OUTPUT_SCHEMA["properties"]["entity_types"]["items"]
    _validator(entity_types_schema).validate(valid_entry)
    with pytest.raises(ValidationError):
        _validator(entity_types_schema).validate({"name": "not_a_real_entity_type", "meaning": "x"})
    with pytest.raises(ValidationError):
        _validator(entity_types_schema).validate({**valid_entry, "extra": True})


def test_describe_graph_output_schema_rejects_a_non_empty_relationship_types_default() -> None:
    defaults_schema = OUTPUT_SCHEMA["properties"]["defaults"]
    path_defaults_schema = defaults_schema["properties"]["graph_path_search"]
    valid = {
        "relationship_types": [],
        "max_depth": 4,
        "max_paths": 10,
        "direction": "both",
    }
    _validator(path_defaults_schema).validate(valid)
    with pytest.raises(ValidationError):
        _validator(path_defaults_schema).validate({**valid, "relationship_types": ["links_to"]})


def test_server_rejects_a_registered_tool_with_no_output_schema() -> None:
    class UndocumentedTool(BaseMcpTool):
        spec = McpToolSpec("undocumented", "undocumented")

        async def call(self, arguments, *, principal_id):
            return {"ok": True}

    with pytest.raises(HarborInvariantError, match="undocumented"):
        McpServer(tools=[UndocumentedTool()])


def test_default_tool_registry_all_declare_an_output_schema() -> None:
    server = McpServer()
    assert server.tools is not None
    for tool in server.tools:
        assert tool.spec.output_schema is not None, tool.spec.name
