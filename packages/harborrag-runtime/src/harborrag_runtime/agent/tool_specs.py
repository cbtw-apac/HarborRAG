"""Canonical read-tool schemas shared by the agent loop and the MCP server.

These builders are the single definition of the retrieval tool surface. The MCP server
renders the same schemas under its own policy bounds rather than restating them, because
the two copies previously drifted -- ``max_nodes`` defaulted to the policy maximum in one
and a hardcoded 20 in the other, equal only by coincidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.chunking import PROJECTED_RELATION_TYPES
from harborrag_core.retrieval import GraphDirection
from harborrag_engine.retrieval import RetrievalLane

_TENANT = {"type": "string", "minLength": 1, "maxLength": 128}
# Only the predicates the projection actually emits. Offering the reserved members too
# would let a caller filter on an edge type that can never match.
_RELATIONS = [item.value for item in PROJECTED_RELATION_TYPES]
_DIRECTIONS = [item.value for item in GraphDirection]
_DEFAULT_MAX_RESULTS = 20

# Every graph tool below takes a node selector. This sentence is the only thing that tells
# a caller how to obtain one, so it is repeated verbatim in each description rather than
# stated once in a place the model may not read.
_SELECTOR_HINT = (
    "Node selectors accept a chunk_id returned by vector_search (chunk IDs and Chunk "
    "node keys are the same value), a node_key from an earlier graph result, or an exact "
    "full node title. Titles are unset on chunk nodes and are never matched partially. "
    "To start from a natural-language question instead, use graph_neighborhood."
)


@dataclass(frozen=True, slots=True)
class RuntimeAgentToolSpec:
    name: str
    description: str
    input_schema: dict[str, object]
    capability: str = "read"


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
            "filters": {"type": "object"},
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
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4},
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
    tenant: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "required": ["tenant_id", "start_node"],
        "properties": {
            "tenant_id": dict(tenant or _TENANT),
            "start_node": {"type": "string", "minLength": 1},
            "relationship_types": _relationship_types(),
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
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


def graph_neighborhood_schema(
    *,
    max_results: int = _DEFAULT_MAX_RESULTS,
    tenant: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "required": ["tenant_id", "query"],
        "properties": {
            "tenant_id": dict(tenant or _TENANT),
            "query": {"type": "string", "minLength": 1},
            "seed_limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
            "relationship_types": _relationship_types(),
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
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
GRAPH_NEIGHBORHOOD_DESCRIPTION = (
    "Expand the tenant knowledge graph around whatever a natural-language question "
    "retrieves. Resolves its own seeds through the vector index, so it needs no node "
    "key -- use this when starting from a question rather than a known node. Returns "
    "the merged neighborhood plus the chunk_id seeds it grew from."
)


RUNTIME_AGENT_TOOL_SPECS = (
    RuntimeAgentToolSpec(
        "vector_search",
        VECTOR_SEARCH_DESCRIPTION,
        vector_search_schema(),
    ),
    RuntimeAgentToolSpec(
        "graph_neighborhood",
        GRAPH_NEIGHBORHOOD_DESCRIPTION,
        graph_neighborhood_schema(),
    ),
    RuntimeAgentToolSpec(
        "graph_triplet_search",
        GRAPH_TRIPLET_DESCRIPTION,
        graph_triplet_schema(),
    ),
    RuntimeAgentToolSpec(
        "graph_path_search",
        GRAPH_PATH_DESCRIPTION,
        graph_path_schema(),
    ),
    RuntimeAgentToolSpec(
        "graph_subgraph_search",
        GRAPH_SUBGRAPH_DESCRIPTION,
        graph_subgraph_schema(),
    ),
)

__all__ = [
    "GRAPH_NEIGHBORHOOD_DESCRIPTION",
    "GRAPH_PATH_DESCRIPTION",
    "GRAPH_SUBGRAPH_DESCRIPTION",
    "GRAPH_TRIPLET_DESCRIPTION",
    "RUNTIME_AGENT_TOOL_SPECS",
    "RuntimeAgentToolSpec",
    "VECTOR_SEARCH_DESCRIPTION",
    "graph_neighborhood_schema",
    "graph_path_schema",
    "graph_subgraph_schema",
    "graph_triplet_schema",
    "vector_search_schema",
]
