"""Bounded read queries over the FalkorDB knowledge graph."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.knowledge_mapping import (
    KnowledgeGraphMapper,
    build_knowledge_traversal,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_node_resolution import (
    resolve_knowledge_node,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_paths import AnchoredPathSearch
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (
    RELATION_IDENTIFIERS,
    access_parameters,
    access_predicate,
    path_limit_for,
    read_retrieval_rows,
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

# How many matching triplets a read may collect before ordering them; see search_triplets.
TRIPLET_SCAN_LIMIT = 500


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
    """Return bounded canonical subject-predicate-object matches.

    The subject and object selectors are resolved to node keys first, through the same
    index-anchored tiers as subgraph and path seeds, and the relationship MATCH then
    starts from a bound node. Matching selectors inline -- as an OR over node_key,
    logical_id, lower-cased title and the support observations' source/target titles --
    left nothing to anchor on, so every call walked every relationship in the tenant and
    timed out. As with node resolution, observation titles are no longer selectors.

    A predicate-only query has no node to start from; it is anchored on the predicate's
    relationship-type index instead, and the endpoint filters sit behind a WITH so the
    planner cannot start from a tenant-wide node scan. Either way the candidates are cut
    at ``TRIPLET_SCAN_LIMIT`` before ordering: sorting every CONTAINS edge of a project
    with 156k issues to return ten of them timed out, so the relation_id order is exact
    whenever the matches fit under the cap and covers the first ``TRIPLET_SCAN_LIMIT``
    otherwise, which ``truncated`` then reports.
    """

    anchors: dict[str, str | None] = {"subject": None, "object": None}
    for role, selector in (("subject", query.subject), ("object", query.object)):
        if selector is None:
            continue
        node = await resolve_knowledge_node(
            database,
            selector,
            access_scope=query.access_scope,
            context=context,
        )
        if node is None:
            return GraphTripletResult(triplets=())
        anchors[role] = node.node_key
    rows = await read_retrieval_rows(
        database,
        _triplet_statement(query, anchor=_triplet_anchor(anchors)),
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "subject_key": anchors["subject"],
            "object_key": anchors["object"],
            "scan_limit": max(TRIPLET_SCAN_LIMIT, query.limit + 1),
            "limit": query.limit + 1,
            **access_parameters(query.access_scope),
        },
    )
    # The scan cap is at least limit + 1, so a cut scan always reports truncation.
    return GraphTripletResult(
        triplets=tuple(KnowledgeGraphMapper.triplet(row) for row in rows[: query.limit]),
        truncated=len(rows) > query.limit,
    )


def _triplet_anchor(anchors: dict[str, str | None]) -> str | None:
    """The bound endpoint to start from; the subject wins when both are known."""

    return next((role for role in ("subject", "object") if anchors[role] is not None), None)


def _triplet_statement(query: GraphTripletQuery, *, anchor: str | None) -> str:
    """Build one triplet read that starts from an index rather than a tenant scan.

    With a bound endpoint the relationship MATCH expands from that node. Without one,
    ``(subject)-[predicate:TYPE]->(object)`` still let the planner start from a node index
    scan of the whole tenant and probe each node for an edge of the type, which timed out
    for every type with few or no edges; scanning the edge index of the type and reading
    its endpoints with startNode/endNode costs only the edges of that type.
    """

    relation_type = (
        f":{RELATION_IDENTIFIERS[query.predicate]}" if query.predicate is not None else ""
    )
    if anchor is not None:
        match = f"""
        MATCH ({anchor}:KnowledgeNode {{
            tenant_id: $tenant_id,
            node_key: ${anchor}_key,
            graph_schema_version: $graph_schema_version
        }})
        WITH {anchor}
        MATCH (subject:KnowledgeNode)-[predicate{relation_type}]->(object:KnowledgeNode)
        WHERE predicate.tenant_id = $tenant_id
          AND predicate.graph_schema_version = $graph_schema_version
          AND {access_predicate("predicate")}
        WITH subject, predicate, object
        """
    else:
        match = f"""
        MATCH ()-[predicate{relation_type}]->()
        WHERE predicate.tenant_id = $tenant_id
          AND predicate.graph_schema_version = $graph_schema_version
          AND {access_predicate("predicate")}
        WITH predicate, startNode(predicate) AS subject, endNode(predicate) AS object
        """
    return f"""
        {match}
        WHERE subject.tenant_id = $tenant_id
          AND object.tenant_id = $tenant_id
          AND subject.graph_schema_version = $graph_schema_version
          AND object.graph_schema_version = $graph_schema_version
          AND {access_predicate("subject")}
          AND {access_predicate("object")}
          AND ($subject_key IS NULL OR subject.node_key = $subject_key)
          AND ($object_key IS NULL OR object.node_key = $object_key)
        WITH subject, predicate, object
        LIMIT $scan_limit
        RETURN subject, predicate, object
        ORDER BY predicate.relation_id
        LIMIT $limit
        """


async def find_paths(
    database: FalkorDBClient,
    query: GraphPathQuery,
    *,
    context: StorageOperationContext,
) -> GraphPathResult:
    """Return bounded explicit paths without exposing provider node IDs."""

    return await AnchoredPathSearch(database).find(query, context=context)


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

    start_node = await resolve_knowledge_node(
        database,
        query.start_node,
        access_scope=query.access_scope,
        context=context,
    )
    nodes: dict[str, GraphNodeRecord] = (
        {} if start_node is None else {start_node.node_key: start_node}
    )

    relations: dict[str, GraphEdgeRecord] = {}
    truncated = False
    frontier = list(nodes.keys())

    for _level in range(query.max_depth):
        if not frontier or len(nodes) >= query.max_nodes:
            break
        remaining = max(query.max_nodes - len(nodes), 1)
        level_limit = path_limit_for(remaining)
        # The WITH between the frontier lookup and the hop is load-bearing: in one MATCH
        # the planner preferred the indexed related.tenant_id equality, scanned every node
        # in the tenant and traversed back to the frontier, and timed out at level two.
        rows = await read_retrieval_rows(
            database,
            f"""
            UNWIND $frontier AS start_key
            MATCH (start:KnowledgeNode {{
                node_key: start_key,
                graph_schema_version: $graph_schema_version,
                tenant_id: $tenant_id
            }})
            WITH start
            MATCH (start){left}[relation]{right}(related:KnowledgeNode)
            WHERE related.tenant_id = $tenant_id
              AND related.graph_schema_version = $graph_schema_version
              AND relation.tenant_id = $tenant_id
              AND relation.graph_schema_version = $graph_schema_version
              AND {access_predicate("relation")}
              AND {access_predicate("related")}
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
                **access_parameters(query.access_scope),
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
