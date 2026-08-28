"""Strict, fully-nested output JSON Schema for ``describe_graph``.

Split out of ``graph_catalog.py`` (which owns the catalog *data*: the enum-keyed
meanings, topologies, workflows, and defaults) so each module stays focused: one
builds the payload's content, this one describes its exact shape.
"""

from __future__ import annotations

from .graph_catalog import (
    CONNECTOR_TOPOLOGIES,
    DIRECTION_VALUES,
    ENTITY_TYPE_MEANINGS,
    NODE_KIND_MEANINGS,
    RELATION_MEANINGS,
    VECTOR_SEARCH_LANE_VALUES,
)


def _named_entry_schema(name_values: list[str]) -> dict[str, object]:
    """Schema for one ``{"name": ..., "meaning": ...}`` catalog entry.

    Unlike a graph node's ``entity_type`` (open, see ``output_schemas.py``), every name
    here comes from ``graph_catalog``'s own static, closed dictionaries -- so ``name``
    can safely enum-constrain to the exact keys this payload actually emits.
    """
    return {
        "type": "object",
        "required": ["name", "meaning"],
        "properties": {
            "name": {"type": "string", "enum": name_values},
            "meaning": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }


_EMPTY_ARRAY_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": 0,
}
_EMPTY_OBJECT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_CAPABILITIES_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "free_text_search",
        "partial_title_matching",
        "tenant_inventory",
        "vector_to_graph_handoff",
        "composed_context_retrieval",
    ],
    "properties": {
        "free_text_search": {"type": "boolean"},
        "partial_title_matching": {"type": "boolean"},
        "tenant_inventory": {"type": "boolean"},
        "vector_to_graph_handoff": {"type": "boolean"},
        "composed_context_retrieval": {"type": "boolean"},
    },
    "additionalProperties": False,
}

_SELECTOR_RULES_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["accepted", "title_matching", "chunk_titles_available", "preferred_entry_tool"],
    "properties": {
        "accepted": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["chunk_id", "node_key", "logical_id", "exact_title"],
            },
        },
        "title_matching": {"type": "string", "enum": ["case_insensitive_exact"]},
        "chunk_titles_available": {"type": "boolean"},
        "preferred_entry_tool": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

_DIRECTION_SEMANTICS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["description", "accepted", "default"],
    "properties": {
        "description": {"type": "string", "minLength": 1},
        "accepted": {"type": "array", "items": {"type": "string", "enum": DIRECTION_VALUES}},
        "default": {"type": "string", "enum": DIRECTION_VALUES},
    },
    "additionalProperties": False,
}

_TOPOLOGY_ENTRY_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["connector", "entity_chain"],
    "properties": {
        "connector": {
            "type": "string",
            "enum": [str(topology["connector"]) for topology in CONNECTOR_TOPOLOGIES],
        },
        "entity_chain": {
            "type": "array",
            "items": {"type": "string", "enum": [entity.value for entity in ENTITY_TYPE_MEANINGS]},
            "minItems": 2,
        },
    },
    "additionalProperties": False,
}

_WORKFLOW_ENTRY_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["name", "use_when", "steps"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "use_when": {"type": "string", "minLength": 1},
        "steps": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
    },
    "additionalProperties": False,
}

_VECTOR_SEARCH_DEFAULTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["top_k", "lane", "filters", "observe_graph", "score_threshold"],
    "properties": {
        "top_k": {"type": "integer"},
        "lane": {"type": "string", "enum": VECTOR_SEARCH_LANE_VALUES},
        "filters": _EMPTY_OBJECT_SCHEMA,
        "observe_graph": {"type": "boolean"},
        "score_threshold": {"type": "number"},
    },
    "additionalProperties": False,
}
_GRAPH_TRIPLET_DEFAULTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["limit"],
    "properties": {"limit": {"type": "integer"}},
    "additionalProperties": False,
}
_GRAPH_PATH_DEFAULTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["relationship_types", "max_depth", "max_paths", "direction"],
    "properties": {
        "relationship_types": _EMPTY_ARRAY_SCHEMA,
        "max_depth": {"type": "integer"},
        "max_paths": {"type": "integer"},
        "direction": {"type": "string", "enum": DIRECTION_VALUES},
    },
    "additionalProperties": False,
}
_GRAPH_SUBGRAPH_DEFAULTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["relationship_types", "max_depth", "max_nodes", "direction"],
    "properties": {
        "relationship_types": _EMPTY_ARRAY_SCHEMA,
        "max_depth": {"type": "integer"},
        "max_nodes": {"type": "integer"},
        "direction": {"type": "string", "enum": DIRECTION_VALUES},
    },
    "additionalProperties": False,
}
_DEFAULTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "vector_search",
        "graph_triplet_search",
        "graph_path_search",
        "graph_subgraph_search",
    ],
    "properties": {
        "vector_search": _VECTOR_SEARCH_DEFAULTS_SCHEMA,
        "graph_triplet_search": _GRAPH_TRIPLET_DEFAULTS_SCHEMA,
        "graph_path_search": _GRAPH_PATH_DEFAULTS_SCHEMA,
        "graph_subgraph_search": _GRAPH_SUBGRAPH_DEFAULTS_SCHEMA,
    },
    "additionalProperties": False,
}

_LIMITS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["maximum_depth", "maximum_results"],
    "properties": {
        "maximum_depth": {"type": "integer"},
        "maximum_results": {"type": "integer"},
    },
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "ok",
        "graph_schema_version",
        "capabilities",
        "selector_rules",
        "node_kinds",
        "entity_types",
        "relation_types",
        "direction_semantics",
        "topologies",
        "workflows",
        "defaults",
        "limits",
    ],
    "properties": {
        "ok": {"const": True},
        "graph_schema_version": {"type": "string", "minLength": 1},
        "capabilities": _CAPABILITIES_SCHEMA,
        "selector_rules": _SELECTOR_RULES_SCHEMA,
        "node_kinds": {
            "type": "array",
            "items": _named_entry_schema([kind.value for kind in NODE_KIND_MEANINGS]),
        },
        "entity_types": {
            "type": "array",
            "items": _named_entry_schema([entity.value for entity in ENTITY_TYPE_MEANINGS]),
        },
        "relation_types": {
            "type": "array",
            "items": _named_entry_schema([relation.value for relation in RELATION_MEANINGS]),
        },
        "direction_semantics": _DIRECTION_SEMANTICS_SCHEMA,
        "topologies": {"type": "array", "items": _TOPOLOGY_ENTRY_SCHEMA},
        "workflows": {"type": "array", "items": _WORKFLOW_ENTRY_SCHEMA},
        "defaults": _DEFAULTS_SCHEMA,
        "limits": _LIMITS_SCHEMA,
    },
    "additionalProperties": False,
}
