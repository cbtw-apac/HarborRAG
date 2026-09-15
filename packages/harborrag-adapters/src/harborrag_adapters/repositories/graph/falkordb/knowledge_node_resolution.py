"""Deterministic selector resolution for tenant-scoped knowledge graph reads."""

from __future__ import annotations

from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION, GraphNodeRecord
from harborrag_core.retrieval import (
    GraphAccessScope,
    GraphNodeResolutionQuery,
    GraphNodeResolutionResult,
    GraphNodeSelectorKind,
)
from harborrag_core.storage import StorageOperationContext

from .client import FalkorDBClient
from .knowledge_mapping import KnowledgeGraphMapper
from .knowledge_support import access_parameters, access_predicate, read_rows


async def resolve_knowledge_node(
    database: FalkorDBClient,
    selector: str,
    *,
    access_scope: GraphAccessScope | None = None,
    context: StorageOperationContext,
) -> GraphNodeRecord | None:
    """Resolve one portable selector without allowing an ambiguous multi-node seed.

    Node keys and logical IDs are the preferred selectors. Titles remain a compatibility
    fallback, including source-title observations carried by support relationships. A
    deterministic first key preserves the existing contract until title ambiguity is
    surfaced explicitly by the public query model.
    """

    rows = await read_rows(
        database,
        f"""
        MATCH (node:KnowledgeNode)
        WHERE node.tenant_id = $tenant_id
          AND node.graph_schema_version = $graph_schema_version
          AND {access_predicate("node")}
        OPTIONAL MATCH (node)-[support]-()
        WHERE support.tenant_id = $tenant_id
          AND support.graph_schema_version = $graph_schema_version
          AND {access_predicate("support")}
        WITH node, collect(support) AS observations
        WHERE (node.node_key = $selector
               OR node.logical_id = $selector
               OR toLower(node.title) = toLower($selector)
               OR any(r IN observations WHERE
                    (r.source_node_key = node.node_key
                     AND toLower(r.source_title) = toLower($selector)) OR
                    (r.target_node_key = node.node_key
                     AND toLower(r.target_title) = toLower($selector))))
        RETURN node
        ORDER BY node.node_key
        LIMIT 1
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "selector": selector,
            **access_parameters(access_scope),
        },
    )
    assert len(rows) <= 1, "node selector resolution must return at most one row"
    return KnowledgeGraphMapper.node(rows[0]["node"]) if rows else None


async def resolve_knowledge_nodes(
    database: FalkorDBClient,
    query: GraphNodeResolutionQuery,
    *,
    context: StorageOperationContext,
) -> GraphNodeResolutionResult:
    """Return every bounded exact match so ambiguity remains visible to callers."""

    selector_clause = {
        GraphNodeSelectorKind.NODE_KEY: "node.node_key = $value",
        GraphNodeSelectorKind.PROVIDER_ID: "node.logical_id = $value",
        GraphNodeSelectorKind.EXACT_TITLE: "node.title_key = $title_key",
    }[query.selector_kind]
    rows = await read_rows(
        database,
        f"""
        MATCH (node:KnowledgeNode)
        WHERE node.tenant_id = $tenant_id
          AND node.graph_schema_version = $graph_schema_version
          AND ({selector_clause})
          AND {access_predicate("node")}
          AND (size($source_scope_ids) = 0 OR node.source_scope_id IN $source_scope_ids)
          AND (size($entity_types) = 0 OR node.entity_type IN $entity_types)
        RETURN node
        ORDER BY node.node_key
        LIMIT $limit
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "value": query.value,
            "title_key": query.value.lower(),
            "source_scope_ids": list(query.source_scope_ids),
            "entity_types": list(query.entity_types),
            "limit": query.limit + 1,
            **access_parameters(query.access_scope),
        },
    )
    return GraphNodeResolutionResult(
        candidates=tuple(KnowledgeGraphMapper.node(row["node"]) for row in rows[: query.limit]),
        truncated=len(rows) > query.limit,
    )
