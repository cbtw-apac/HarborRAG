"""Endpoint-anchored path search for FalkorDB knowledge projections."""

from __future__ import annotations

from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION
from harborrag_core.retrieval import GraphPathQuery, GraphPathResult
from harborrag_core.storage import StorageOperationContext

from ..traversal import GraphTraversalSyntax
from .client import FalkorDBClient
from .knowledge_mapping import KnowledgeGraphMapper
from .knowledge_node_resolution import resolve_knowledge_node
from .knowledge_support import access_parameters, access_predicate, read_rows


class AnchoredPathSearch:
    """Find bounded paths after reducing both selectors to exact indexed node keys.

    Applying title/key predicates after a variable-length MATCH makes FalkorDB enumerate
    tenant-wide walks before it can discard unrelated endpoints. Resolving both endpoints
    first keeps the variable expansion anchored and makes the same query scale with the
    relevant neighborhood.
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
        if end is None:
            return GraphPathResult(paths=())

        left, right = GraphTraversalSyntax.arrows(query.direction)
        rows = await read_rows(
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
            MATCH path=(start){left}[*1..{query.max_depth}]{right}(end)
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
            ORDER BY size(path_relations)
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
