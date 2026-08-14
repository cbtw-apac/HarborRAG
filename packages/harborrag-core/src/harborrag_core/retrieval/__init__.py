"""Provider-independent retrieval request and result contracts."""

from .graph import (
    GraphDirection,
    GraphPath,
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
    GraphTripletResult,
    compact_node,
    compact_path,
    compact_relation,
    compact_triplet,
)

__all__ = [
    "GraphDirection",
    "GraphPath",
    "GraphPathQuery",
    "GraphPathResult",
    "GraphSubgraphQuery",
    "GraphTriplet",
    "GraphTripletQuery",
    "GraphTripletResult",
    "compact_node",
    "compact_path",
    "compact_relation",
    "compact_triplet",
]
