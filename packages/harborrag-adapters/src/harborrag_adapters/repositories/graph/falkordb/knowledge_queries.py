"""Bounded read queries over the FalkorDB knowledge graph."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.knowledge_mapping import (
    KnowledgeGraphMapper,
    build_knowledge_traversal,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (
    path_limit_for,
    read_rows,
)
from harborrag_adapters.repositories.graph.traversal import GraphTraversalSyntax
from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION, KnowledgeGraphTraversal
from harborrag_core.retrieval import (
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext

# Mirrors the Field(ge=..., le=...) bounds on GraphPathQuery/GraphSubgraphQuery in
# harborrag_core.retrieval.graph. traverse() is reached without one of those query models,
# so TraversalBounds below re-states the same envelope for that path.
MIN_TRAVERSAL_DEPTH = 1
MAX_TRAVERSAL_DEPTH = 8
MIN_TRAVERSAL_NODES = 1
MAX_TRAVERSAL_NODES = 5_000


@dataclass(frozen=True, slots=True)
class TraversalBounds:
    """Envelope for one bounded traversal, validated on construction."""

    max_depth: int
    max_nodes: int
    direction: str

    def __post_init__(self) -> None:
        if not MIN_TRAVERSAL_DEPTH <= self.max_depth <= MAX_TRAVERSAL_DEPTH:
            raise ValueError(
                f"graph traversal depth must be between "
                f"{MIN_TRAVERSAL_DEPTH} and {MAX_TRAVERSAL_DEPTH}"
            )
        if not MIN_TRAVERSAL_NODES <= self.max_nodes <= MAX_TRAVERSAL_NODES:
            raise ValueError(
                f"graph traversal max_nodes must be between "
                f"{MIN_TRAVERSAL_NODES} and {MAX_TRAVERSAL_NODES}"
            )


async def traverse(
    database: FalkorDBClient,
    start_node_key: str,
    *,
    bounds: TraversalBounds,
    context: StorageOperationContext,
) -> KnowledgeGraphTraversal:
    """Traverse a bounded graph without exposing provider node IDs."""

    max_depth, max_nodes, direction = bounds.max_depth, bounds.max_nodes, bounds.direction
    if not start_node_key.strip():
        raise ValueError("graph traversal start_node_key must be non-empty")
    left, right = GraphTraversalSyntax.arrows(direction)
    path_limit = path_limit_for(max_nodes)
    rows = await read_rows(
        database,
        f"""
        MATCH path=(start:KnowledgeNode){left}[*1..{max_depth}]
                   {right}(related:KnowledgeNode)
        WHERE start.tenant_id = $tenant_id
          AND start.node_key = $start_node_key
          AND start.graph_schema_version = $graph_schema_version
          AND all(node IN nodes(path) WHERE node.tenant_id = $tenant_id
                  AND node.graph_schema_version = $graph_schema_version)
          AND all(relation IN relationships(path)
                  WHERE relation.tenant_id = $tenant_id
                    AND relation.graph_schema_version = $graph_schema_version)
        RETURN nodes(path) AS path_nodes,
               relationships(path) AS path_relations
        LIMIT $path_limit
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "start_node_key": start_node_key,
            "path_limit": path_limit + 1,
        },
    )
    return build_knowledge_traversal(rows, max_nodes=max_nodes, path_limit=path_limit)


async def search_triplets(
    database: FalkorDBClient,
    query: GraphTripletQuery,
    *,
    context: StorageOperationContext,
) -> GraphTripletResult:
    """Return bounded canonical subject-predicate-object matches."""

    rows = await read_rows(
        database,
        """
        MATCH (subject:KnowledgeNode)-[predicate]->(object:KnowledgeNode)
        WHERE subject.tenant_id = $tenant_id
          AND object.tenant_id = $tenant_id
          AND predicate.tenant_id = $tenant_id
          AND subject.graph_schema_version = $graph_schema_version
          AND object.graph_schema_version = $graph_schema_version
          AND predicate.graph_schema_version = $graph_schema_version
          AND ($subject IS NULL
               OR subject.node_key = $subject
               OR subject.logical_id = $subject
               OR toLower(subject.title) = toLower($subject))
          AND ($predicate IS NULL OR predicate.relation_type = $predicate)
          AND ($object IS NULL
               OR object.node_key = $object
               OR object.logical_id = $object
               OR toLower(object.title) = toLower($object))
        RETURN subject, predicate, object
        ORDER BY predicate.relation_id
        LIMIT $limit
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "subject": query.subject,
            "predicate": query.predicate.value if query.predicate is not None else None,
            "object": query.object,
            "limit": query.limit + 1,
        },
    )
    return GraphTripletResult(
        triplets=tuple(KnowledgeGraphMapper.triplet(row) for row in rows[: query.limit]),
        truncated=len(rows) > query.limit,
    )


async def find_paths(
    database: FalkorDBClient,
    query: GraphPathQuery,
    *,
    context: StorageOperationContext,
) -> GraphPathResult:
    """Return bounded explicit paths without exposing provider node IDs."""

    left, right = GraphTraversalSyntax.arrows(query.direction)
    rows = await read_rows(
        database,
        f"""
        MATCH path=(start:KnowledgeNode){left}[*1..{query.max_depth}]
                   {right}(end:KnowledgeNode)
        WHERE start.tenant_id = $tenant_id
          AND end.tenant_id = $tenant_id
          AND start.graph_schema_version = $graph_schema_version
          AND end.graph_schema_version = $graph_schema_version
          AND (start.node_key = $start_node
               OR start.logical_id = $start_node
               OR toLower(start.title) = toLower($start_node))
          AND (end.node_key = $end_node
               OR end.logical_id = $end_node
               OR toLower(end.title) = toLower($end_node))
          AND all(node IN nodes(path) WHERE node.tenant_id = $tenant_id
                  AND node.graph_schema_version = $graph_schema_version)
          AND all(relation IN relationships(path)
                  WHERE relation.tenant_id = $tenant_id
                    AND relation.graph_schema_version = $graph_schema_version
                    AND (size($relationship_types) = 0
                         OR relation.relation_type IN $relationship_types))
        RETURN nodes(path) AS path_nodes,
               relationships(path) AS path_relations
        ORDER BY size(path_relations)
        LIMIT $max_paths
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "start_node": query.start_node,
            "end_node": query.end_node,
            "relationship_types": [item.value for item in query.relationship_types],
            "max_paths": query.max_paths + 1,
        },
    )
    return GraphPathResult(
        paths=tuple(KnowledgeGraphMapper.path(row) for row in rows[: query.max_paths]),
        truncated=len(rows) > query.max_paths,
    )


async def expand_subgraph(
    database: FalkorDBClient,
    query: GraphSubgraphQuery,
    *,
    context: StorageOperationContext,
) -> KnowledgeGraphTraversal:
    """Expand a bounded filtered neighborhood from a portable selector."""

    left, right = GraphTraversalSyntax.arrows(query.direction)
    path_limit = path_limit_for(query.max_nodes)
    rows = await read_rows(
        database,
        f"""
        MATCH path=(start:KnowledgeNode){left}[*1..{query.max_depth}]
                   {right}(related:KnowledgeNode)
        WHERE start.tenant_id = $tenant_id
          AND start.graph_schema_version = $graph_schema_version
          AND (start.node_key = $start_node
               OR start.logical_id = $start_node
               OR toLower(start.title) = toLower($start_node))
          AND all(node IN nodes(path) WHERE node.tenant_id = $tenant_id
                  AND node.graph_schema_version = $graph_schema_version)
          AND all(relation IN relationships(path)
                  WHERE relation.tenant_id = $tenant_id
                    AND relation.graph_schema_version = $graph_schema_version
                    AND (size($relationship_types) = 0
                         OR relation.relation_type IN $relationship_types))
        RETURN nodes(path) AS path_nodes,
               relationships(path) AS path_relations
        LIMIT $path_limit
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "start_node": query.start_node,
            "relationship_types": [item.value for item in query.relationship_types],
            "path_limit": path_limit + 1,
        },
    )
    return build_knowledge_traversal(rows, max_nodes=query.max_nodes, path_limit=path_limit)
