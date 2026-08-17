"""Reviewed edge-shape vocabulary of graph schema v2."""

from __future__ import annotations

# A signature observed but absent here is a schema change: trace it to its builder code
# path, review it, then add it. The engine signature test locks what the projection *can*
# emit against this set; the runtime graph-eval corpus asserts equality with what it
# *does* emit, so both import this single copy.
PROJECTED_EDGE_SIGNATURES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("Tenant", "has_data_source", "DataSource"),
        ("DataSource", "contains", "SourceEntity"),
        ("SourceEntity", "has_version", "DocumentVersion"),
        ("DocumentVersion", "contains", "Structure"),
        ("Structure", "parent_of", "Structure"),
        ("Structure", "contains", "Structure"),
        ("Structure", "links_to", "Structure"),
        ("Structure", "reply_to", "Structure"),
        ("Chunk", "supports", "Structure"),
        ("Chunk", "supports", "DocumentVersion"),
        ("SourceEntity", "links_to", "SourceEntity"),
        ("SourceEntity", "parent_of", "SourceEntity"),
        ("SourceEntity", "blocks", "SourceEntity"),
        ("SourceEntity", "duplicates", "SourceEntity"),
        ("SourceEntity", "relates_to", "SourceEntity"),
        ("SourceEntity", "has_attachment", "SourceEntity"),
        ("SourceEntity", "contains", "SourceEntity"),
        ("SourceEntity", "points_to", "SourceEntity"),
        ("DocumentVersion", "resolved_at", "SourceEntity"),
    }
)
