"""Provider-independent retrieval request and result contracts."""

from .graph import (
    GraphDirection,
    GraphNeighborhoodQuery,
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
    "GraphNeighborhoodQuery",
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
