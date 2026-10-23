"""Endpoint-anchored path search for FalkorDB knowledge projections."""

from __future__ import annotations

from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION
from harborrag_core.retrieval import GraphPathQuery, GraphPathResult
from harborrag_core.storage import StorageOperationContext

from ..traversal import GraphTraversalSyntax
from .client import FalkorDBClient
from .knowledge_mapping import KnowledgeGraphMapper
from .knowledge_node_resolution import resolve_knowledge_node
from .knowledge_support import (
    RELATION_IDENTIFIERS,
    access_parameters,
    access_predicate,
    read_retrieval_rows,
)


class AnchoredPathSearch:
    """Find bounded shortest paths after reducing both selectors to exact node keys.

    Applying title/key predicates after a variable-length MATCH makes FalkorDB enumerate
    tenant-wide walks before it can discard unrelated endpoints, so both endpoints are
    resolved first and the expansion starts from two bound nodes.

    Binding the endpoints is not enough on its own. ``MATCH (start)-[*1..d]-(end)`` with
    ``ORDER BY size(path)`` still enumerates every walk of length up to ``d`` before it
    can sort, and one hub on the way -- a project CONTAINS every issue -- turns that into
    hundreds of thousands of walks: on a 156k-document tenant even two attachments of the
    same issue (distance 2) timed out. ``allShortestPaths`` is a breadth-first search
    that visits each node once and stops at the first depth that reaches ``end``, so its
    cost is bounded by the neighborhood within that depth rather than by the number of
    walks through it. The paths it yields all have the minimal length, which is also why
    no ORDER BY is needed.

    Behavior change: only shortest paths (up to ``max_depth``) are returned, not every
    path up to ``max_depth``. Relationship types are part of the pattern so the search
    honors them while expanding; the ACL is a filter over each shortest path, so when
    every shortest path crosses a hidden node or relation the result is empty even if a
    longer visible path exists.
    """

    def __init__(self, database: FalkorDBClient) -> None:
        self._database = database

    async def find(
        self,
        query: GraphPathQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphPathResult:
        start = await resolve_knowledge_node(
            self._database,
            query.start_node,
            access_scope=query.access_scope,
            context=context,
        )
        if start is None:
            return GraphPathResult(paths=())
        end = await resolve_knowledge_node(
            self._database,
            query.end_node,
            access_scope=query.access_scope,
            context=context,
        )
        if end is None or end.node_key == start.node_key:
            # allShortestPaths has no non-empty path from a node to itself.
            return GraphPathResult(paths=())

        left, right = GraphTraversalSyntax.arrows(query.direction)
        types = "|".join(sorted({RELATION_IDENTIFIERS[item] for item in query.relationship_types}))
        type_filter = f":{types}" if types else ""
        rows = await read_retrieval_rows(
            self._database,
            f"""
            MATCH (start:KnowledgeNode {{
                tenant_id: $tenant_id,
                node_key: $start_node_key,
                graph_schema_version: $graph_schema_version
            }})
            MATCH (end:KnowledgeNode {{
                tenant_id: $tenant_id,
                node_key: $end_node_key,
                graph_schema_version: $graph_schema_version
            }})
            WITH start, end
            MATCH path=allShortestPaths(
                (start){left}[{type_filter}*1..{query.max_depth}]{right}(end)
            )
            WHERE all(node IN nodes(path) WHERE node.tenant_id = $tenant_id
                      AND node.graph_schema_version = $graph_schema_version
                      AND {access_predicate("node")})
              AND all(relation IN relationships(path)
                      WHERE relation.tenant_id = $tenant_id
                        AND relation.graph_schema_version = $graph_schema_version
                        AND {access_predicate("relation")}
                        AND (size($relationship_types) = 0
                             OR relation.relation_type IN $relationship_types))
            RETURN nodes(path) AS path_nodes,
                   relationships(path) AS path_relations
            LIMIT $max_paths
            """,
            {
                "tenant_id": str(context.tenant_id),
                "graph_schema_version": GRAPH_SCHEMA_VERSION,
                "start_node_key": start.node_key,
                "end_node_key": end.node_key,
                "relationship_types": [item.value for item in query.relationship_types],
                "max_paths": query.max_paths + 1,
                **access_parameters(query.access_scope),
            },
        )
        return GraphPathResult(
            paths=tuple(KnowledgeGraphMapper.path(row) for row in rows[: query.max_paths]),
            truncated=len(rows) > query.max_paths,
        )
