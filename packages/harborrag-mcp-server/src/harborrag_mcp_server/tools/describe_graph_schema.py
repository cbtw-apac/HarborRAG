"""Strict, fully-nested output JSON Schema for ``describe_graph``.

Split out of ``graph_catalog.py`` (which owns the catalog *data*: schema versions,
node/relation names, and property lists) so each module stays focused: one builds the
payload's content, this one describes its exact shape.
"""

from __future__ import annotations

from .graph_catalog import (
    CHUNK_PROPERTIES,
    COMMON_NODE_PROPERTIES,
    DOCUMENT_OWNED_PROPERTIES,
    ENTITY_PROPERTIES,
    GRAPH_NODE_KINDS,
    GRAPH_RELATION_TYPES,
    RELATES_PROPERTIES,
)


def _closed_string_list_schema(values: list[str]) -> dict[str, object]:
    """Schema for an array whose members must come from a known, closed set.

    Every list here comes from ``graph_catalog``'s own static data, so ``items`` can
    safely enum-constrain to the exact values this payload actually emits.
    """
    return {
        "type": "array",
        "items": {"type": "string", "enum": values},
    }


_VERSIONS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["structural", "semantic", "ontology"],
    "properties": {
        "structural": {"type": "string", "minLength": 1},
        "semantic": {"type": "string", "minLength": 1},
        "ontology": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

_LAYERS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["nodes", "relations"],
    "properties": {
        "nodes": _closed_string_list_schema(GRAPH_NODE_KINDS),
        "relations": _closed_string_list_schema(GRAPH_RELATION_TYPES),
    },
    "additionalProperties": False,
}

_PROPERTIES_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["common_node", "document_owned", "Chunk", "Entity", "RELATES"],
    "properties": {
        "common_node": _closed_string_list_schema(COMMON_NODE_PROPERTIES),
        "document_owned": _closed_string_list_schema(DOCUMENT_OWNED_PROPERTIES),
        "Chunk": _closed_string_list_schema(CHUNK_PROPERTIES),
        "Entity": _closed_string_list_schema(ENTITY_PROPERTIES),
        "RELATES": _closed_string_list_schema(RELATES_PROPERTIES),
    },
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    # describe_graph always returns the full, unfiltered static contract, so every
    # section is required.
    "required": ["ok", "versions", "layers", "properties"],
    "properties": {
        "ok": {"const": True},
        "versions": _VERSIONS_SCHEMA,
        "layers": _LAYERS_SCHEMA,
        "properties": _PROPERTIES_SCHEMA,
    },
    "additionalProperties": False,
}
