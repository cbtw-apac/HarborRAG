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
from harborrag_core.ingestion import (
    GRAPH_SCHEMA_VERSION,
    GraphEdgeRecord,
    GraphNodeRecord,
    KnowledgeGraphTraversal,
)
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
        ORDER BY size(path_relations)
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
    """Expand a bounded filtered neighborhood one hop at a time.

    A single variable-length MATCH ...-[*1..max_depth]-... forces the engine to enumerate
    nearly every walk up to max_depth before it can sort and apply LIMIT, so cost grows
    combinatorially with the branching factor at the start node. On a densely connected
    tenant this times out well before max_depth reaches its documented upper bound (8).
    Expanding one hop per round trip instead bounds every query to a single-hop pattern
    match from an already-known frontier, so total work scales with max_nodes rather than
    with max_depth, and "closest node first" falls out of the level order for free instead
    of needing an ORDER BY over the full search space.
    """

    tenant_id = str(context.tenant_id)
    relationship_types = [item.value for item in query.relationship_types]
    left, right = GraphTraversalSyntax.arrows(query.direction)

    start_rows = await read_rows(
        database,
        """
        MATCH (start:KnowledgeNode)
        WHERE start.tenant_id = $tenant_id
          AND start.graph_schema_version = $graph_schema_version
          AND (start.node_key = $start_node
               OR start.logical_id = $start_node
               OR toLower(start.title) = toLower($start_node))
        RETURN start AS node
        ORDER BY start.node_key
        LIMIT 1
        """,
        {
            "tenant_id": tenant_id,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "start_node": query.start_node,
        },
    )
    assert len(start_rows) <= 1, "start resolution must be constrained to a single row"

    nodes: dict[str, GraphNodeRecord] = {}
    for row in start_rows:
        node = KnowledgeGraphMapper.node(row["node"])
        nodes[node.node_key] = node

    relations: dict[str, GraphEdgeRecord] = {}
    truncated = False
    frontier = list(nodes.keys())

    for _level in range(query.max_depth):
        if not frontier or len(nodes) >= query.max_nodes:
            break
        remaining = max(query.max_nodes - len(nodes), 1)
        level_limit = path_limit_for(remaining)
        rows = await read_rows(
            database,
            f"""
            UNWIND $frontier AS start_key
            MATCH (start:KnowledgeNode {{
                node_key: start_key,
                graph_schema_version: $graph_schema_version,
                tenant_id: $tenant_id
            }}){left}[relation]{right}(related:KnowledgeNode)
            WHERE related.tenant_id = $tenant_id
              AND related.graph_schema_version = $graph_schema_version
              AND relation.tenant_id = $tenant_id
              AND relation.graph_schema_version = $graph_schema_version
              AND (size($relationship_types) = 0
                   OR relation.relation_type IN $relationship_types)
            RETURN relation, related
            LIMIT $level_limit
            """,
            {
                "tenant_id": tenant_id,
                "graph_schema_version": GRAPH_SCHEMA_VERSION,
                "frontier": frontier,
                "relationship_types": relationship_types,
                "level_limit": level_limit + 1,
            },
        )
        if len(rows) > level_limit:
            truncated = True

        next_frontier: list[str] = []
        for row in rows[:level_limit]:
            relation = KnowledgeGraphMapper.relation(row["relation"])
            relations[relation.relation_id] = relation
            related = KnowledgeGraphMapper.node(row["related"])
            if related.node_key in nodes:
                continue
            if len(nodes) >= query.max_nodes:
                truncated = True
                continue
            nodes[related.node_key] = related
            next_frontier.append(related.node_key)
        frontier = next_frontier

    if len(nodes) >= query.max_nodes:
        truncated = True

    valid_relations = tuple(
        relation
        for relation in relations.values()
        if relation.source_node_key in nodes and relation.target_node_key in nodes
    )
    return KnowledgeGraphTraversal(
        nodes=tuple(nodes.values()),
        relations=valid_relations,
        truncated=truncated,
    )
