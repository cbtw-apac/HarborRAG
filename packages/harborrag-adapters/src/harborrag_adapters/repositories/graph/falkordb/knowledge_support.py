"""Shared constants and row access for the FalkorDB knowledge-graph modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.retrieval import GraphAccessScope

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


def access_predicate(variable: str) -> str:
    """Return the fixed Cypher ACL applied before ordering and limiting graph rows."""

    return f"""(
        $access_unrestricted
        OR ({variable}.document_id IS NOT NULL
            AND {variable}.document_id IN $authorized_document_ids)
        OR ({variable}.document_id IS NULL
            AND {variable}.source_scope_id IS NOT NULL
            AND {variable}.source_scope_id IN $authorized_source_scope_ids)
        OR ({variable}.document_id IS NULL
            AND {variable}.source_scope_id IS NULL
            AND {variable}.ownership_scope = 'tenant'
            AND $tenant_visible)
    )"""


def access_parameters(scope: GraphAccessScope | None) -> dict[str, Any]:
    """Encode an absent legacy scope distinctly from an explicit deny-all scope."""

    return {
        "access_unrestricted": scope is None,
        "authorized_document_ids": list(scope.document_ids) if scope is not None else [],
        "authorized_source_scope_ids": (list(scope.source_scope_ids) if scope is not None else []),
        "tenant_visible": scope.tenant_visible if scope is not None else True,
    }


async def read_rows(
    database: FalkorDBClient,
    statement: str,
    parameters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Execute a read statement and decode it into plain row mappings."""

    return FalkorDBMapper.rows(await database.read(statement, parameters))
