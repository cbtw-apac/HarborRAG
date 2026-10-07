"""Deterministic selector resolution for tenant-scoped knowledge graph reads."""

from __future__ import annotations

from typing import Any

from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION, GraphNodeRecord, KnowledgeNodeKind
from harborrag_core.retrieval import (
    GraphAccessScope,
    GraphNodeResolutionQuery,
    GraphNodeResolutionResult,
    GraphNodeSelectorKind,
)
from harborrag_core.storage import StorageOperationContext

from .client import FalkorDBClient
from .knowledge_mapping import KnowledgeGraphMapper
from .knowledge_support import access_parameters, access_predicate, read_retrieval_rows

# The indexed identity properties a portable selector is tried against, most specific
# first, each paired with the expression its value is compared to. node_key is unique per
# tenant. logical_id names one entity but repeats across the versions of one document (a
# DocumentVersion's logical_id is its document_id, a comment's is its provider id).
# title_key repeats freely; comparing it to toLower($selector) reuses the exact Cypher
# normalization the write path stores it with ("node.title_key = toLower(node.title)"),
# so the two can never disagree on a non-ASCII title.
_SELECTOR_TIERS = (
    ("node_key", "$selector"),
    ("logical_id", "$selector"),
    ("title_key", "toLower($selector)"),
)

# A selector matching more nodes than this is not a usable single seed: a section title
# such as "Overview" exists once per document, and ordering every match to pick the
# "first" key would read hundreds of thousands of nodes on a large tenant just to return
# an arbitrary one. Above the cap the tier yields nothing and resolution moves on, so the
# result is either a deterministic first key over every match or no seed at all.
MAX_SELECTOR_CANDIDATES = 32


def _selector_statement(property_name: str, value_expression: str) -> str:
    """One index-anchored resolution tier, bounded before it is ordered.

    The equality on an indexed property is what lets FalkorDB start from a
    ``Node By Index Scan`` of the matching nodes instead of every node in the tenant.
    The ACL is applied before the candidate cap so hidden nodes cannot exhaust it.
    Source entities sort ahead of version-owned nodes: a provider id such as a Jira
    attachment id can also be the logical_id of an unrelated comment structure, and the
    shared source entity is the one the provider id actually names.
    """

    return f"""
        MATCH (node:KnowledgeNode)
        WHERE node.{property_name} = {value_expression}
          AND node.tenant_id = $tenant_id
          AND node.graph_schema_version = $graph_schema_version
          AND {access_predicate("node")}
        WITH node
        LIMIT $candidate_limit
        WITH collect(node) AS candidates
        WHERE size(candidates) <= $max_candidates
        UNWIND candidates AS node
        RETURN node
        ORDER BY CASE node.node_kind WHEN $preferred_node_kind THEN 0 ELSE 1 END,
                 node.node_key
        LIMIT 1
        """


async def resolve_knowledge_node(
    database: FalkorDBClient,
    selector: str,
    *,
    access_scope: GraphAccessScope | None = None,
    context: StorageOperationContext,
) -> GraphNodeRecord | None:
    """Resolve one portable selector without allowing an ambiguous multi-node seed.

    Tiers are tried in order -- node_key (which is also the chunk_id of a Chunk node),
    logical_id (a provider id such as a Jira issue key, or a document_id, which is the
    logical_id of its DocumentVersion nodes), then the store-derived title_key -- and the
    first tier with a match wins, so a later tier is only read when the earlier ones
    found nothing. Within a tier the first node_key is chosen deterministically, as
    before.

    Behavior change: resolution no longer falls back to the ``source_title`` /
    ``target_title`` observations carried on support relationships. Matching those meant
    materializing every node in the tenant together with all of its relationships before
    filtering by the selector, which timed out on every call against a large tenant, and
    no index can anchor it. Display titles of shared source entities are therefore not
    selectors; their logical_id is (a shared entity's stored title is its logical_id).
    A title shared by more than ``MAX_SELECTOR_CANDIDATES`` visible nodes resolves to
    nothing rather than to an arbitrary one of them.
    """

    parameters: dict[str, Any] = {
        "tenant_id": str(context.tenant_id),
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "selector": selector,
        "preferred_node_kind": KnowledgeNodeKind.SOURCE_ENTITY.value,
        "candidate_limit": MAX_SELECTOR_CANDIDATES + 1,
        "max_candidates": MAX_SELECTOR_CANDIDATES,
        **access_parameters(access_scope),
    }
    for property_name, value_expression in _SELECTOR_TIERS:
        rows = await read_retrieval_rows(
            database,
            _selector_statement(property_name, value_expression),
            parameters,
        )
        assert len(rows) <= 1, "node selector resolution must return at most one row"
        if rows:
            return KnowledgeGraphMapper.node(rows[0]["node"])
    return None


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
    rows = await read_retrieval_rows(
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
