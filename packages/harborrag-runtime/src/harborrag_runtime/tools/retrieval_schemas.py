"""Canonical retrieval input schema builders for the shared read-tool catalog.

These builders are the single definition of the retrieval tool surface. The MCP server
renders the same schemas under its own policy bounds rather than restating them, because
the two copies previously drifted -- ``max_nodes`` defaulted to the policy maximum in one
and a hardcoded 20 in the other, equal only by coincidence.
"""

from __future__ import annotations

from harborrag_core.chunking import PROJECTED_RELATION_TYPES
from harborrag_core.retrieval import GraphDirection
from harborrag_core.topology.search import RetrievalMode
from harborrag_engine.retrieval import RetrievalLane

from .base import MAX_TOOL_RESULTS

_TENANT = {"type": "string", "minLength": 1, "maxLength": 128}
# Only the predicates the projection actually emits. Offering the reserved members too
# would let a caller filter on an edge type that can never match.
_RELATIONS = [item.value for item in PROJECTED_RELATION_TYPES]
_DIRECTIONS = [item.value for item in GraphDirection]
_DEFAULT_MAX_RESULTS = MAX_TOOL_RESULTS

# Every graph tool below takes a node selector. This sentence is the only thing that tells
# a caller how to obtain one, so it is repeated verbatim in each description rather than
# stated once in a place the model may not read.
_SELECTOR_HINT = (
    "Node selectors accept a chunk_id returned by vector_search (chunk IDs and Chunk "
    "node keys are the same value), a node_key from an earlier graph result, or an exact "
    "full node title. Titles are unset on chunk nodes and are never matched partially."
)


def _relationship_types() -> dict[str, object]:
    return {
        "type": "array",
        "items": {"type": "string", "enum": list(_RELATIONS)},
        "uniqueItems": True,
        "default": [],
    }


def vector_search_schema(
    *,
    max_results: int = _DEFAULT_MAX_RESULTS,
    tenant: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "required": ["query", "tenant_id"],
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "tenant_id": dict(tenant or _TENANT),
            "top_k": {"type": "integer", "minimum": 1, "maximum": max_results, "default": 5},
            "lane": {
                "type": "string",
                "enum": [item.value for item in RetrievalLane],
                "default": RetrievalLane.HYBRID.value,
            },
            "filters": {"type": "object", "not": {"required": ["tenant_id"]}, "default": {}},
            "mode": {
                "type": "string",
                "enum": [mode.value for mode in RetrievalMode],
                "default": RetrievalMode.FLAT.value,
            },
            "observe_graph": {"type": "boolean", "default": False},
            "include_content": {"type": "boolean", "default": True},
            "score_threshold": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": 0.0,
            },
        },
        "additionalProperties": False,
    }


def graph_triplet_schema(
    *,
    max_results: int = _DEFAULT_MAX_RESULTS,
    tenant: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "required": ["tenant_id"],
        "properties": {
            "tenant_id": dict(tenant or _TENANT),
            "subject": {"type": "string", "minLength": 1},
            "predicate": {"type": "string", "enum": list(_RELATIONS)},
            "object": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": max_results, "default": 10},
        },
        "anyOf": [
            {"required": ["subject"]},
            {"required": ["predicate"]},
            {"required": ["object"]},
        ],
        "additionalProperties": False,
    }


def graph_path_schema(
    *,
    max_results: int = _DEFAULT_MAX_RESULTS,
    max_depth: int = 8,
    tenant: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "required": ["tenant_id", "start_node", "end_node"],
        "properties": {
            "tenant_id": dict(tenant or _TENANT),
            "start_node": {"type": "string", "minLength": 1},
            "end_node": {"type": "string", "minLength": 1},
            "relationship_types": _relationship_types(),
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": max_depth,
                "default": min(4, max_depth),
            },
            "max_paths": {
                "type": "integer",
                "minimum": 1,
                "maximum": max_results,
                "default": 10,
            },
            "direction": {
                "type": "string",
                "enum": list(_DIRECTIONS),
                "default": GraphDirection.BOTH.value,
            },
        },
        "additionalProperties": False,
    }


def graph_subgraph_schema(
    *,
    max_results: int = _DEFAULT_MAX_RESULTS,
    max_depth: int = 8,
    tenant: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "required": ["tenant_id", "start_node"],
        "properties": {
            "tenant_id": dict(tenant or _TENANT),
            "start_node": {"type": "string", "minLength": 1},
            "relationship_types": _relationship_types(),
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": max_depth,
                "default": min(2, max_depth),
            },
            "max_nodes": {
                "type": "integer",
                "minimum": 1,
                "maximum": max_results,
                "default": max_results,
            },
            "direction": {
                "type": "string",
                "enum": list(_DIRECTIONS),
                "default": GraphDirection.BOTH.value,
            },
        },
        "additionalProperties": False,
    }


VECTOR_SEARCH_DESCRIPTION = (
    "Search tenant-scoped indexed evidence for a natural-language query. Each result "
    "carries a chunk_id that doubles as a graph node key for the graph tools."
)
GRAPH_TRIPLET_DESCRIPTION = (
    f"Find active subject-predicate-object records in the tenant knowledge graph. {_SELECTOR_HINT}"
)
GRAPH_PATH_DESCRIPTION = (
    f"Find active graph paths between two tenant-scoped nodes. Defaults to an "
    f"undirected walk, because the spine is not uniformly directed. {_SELECTOR_HINT}"
)
GRAPH_SUBGRAPH_DESCRIPTION = (
    f"Expand an active tenant-scoped graph neighborhood from one known node. {_SELECTOR_HINT}"
)
