"""Shared constants and row access for the FalkorDB knowledge-graph modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import KnowledgeNodeKind

NODE_LABELS = {
    KnowledgeNodeKind.TENANT: "Tenant",
    KnowledgeNodeKind.DATA_SOURCE: "DataSource",
    KnowledgeNodeKind.SOURCE_ENTITY: "SourceEntity",
    KnowledgeNodeKind.DOCUMENT_VERSION: "DocumentVersion",
    KnowledgeNodeKind.STRUCTURE: "Structure",
    KnowledgeNodeKind.CHUNK: "Chunk",
}
RELATION_IDENTIFIERS = {
    relation_type: FalkorDBMapper.safe_identifier(relation_type.value)
    for relation_type in RelationType
}

# A bounded traversal asks for more paths than nodes because one path contributes several
# nodes and many paths revisit nodes already seen. Four paths per requested node keeps the
# read wide enough to fill max_nodes after deduplication without unbounded fan-out.
PATH_LIMIT_FACTOR = 4


def path_limit_for(max_nodes: int) -> int:
    """Return how many paths to read to satisfy a node-bounded traversal."""

    return max_nodes * PATH_LIMIT_FACTOR


async def read_rows(
    database: FalkorDBClient,
    statement: str,
    parameters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Execute a read statement and decode it into plain row mappings."""

    return FalkorDBMapper.rows(await database.read(statement, parameters))
