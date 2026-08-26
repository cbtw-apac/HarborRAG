"""Tenant-scoped FalkorDB graph read operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_adapters.repositories.graph.traversal import GraphTraversalSyntax
from harborrag_core.schemas.graph import (
    GraphEdge,
    GraphExpansionQuery,
    GraphNode,
    GraphSubgraph,
)
from harborrag_core.schemas.ids import EntityId, RelationshipId
from harborrag_core.storage import StorageOperationContext


class FalkorDBGraphReader:
    """Read normalized nodes, edges, and bounded expansions from FalkorDB."""

    def __init__(self, database: FalkorDBClient) -> None:
        self._database = database

    async def get_nodes(
        self,
        ids: Sequence[EntityId],
        context: StorageOperationContext,
    ) -> list[GraphNode]:
        rows = await self._read(
            """
            MATCH (n:HarborEntity)
            WHERE n.tenant_id = $tenant_id AND n.id IN $ids
            RETURN n AS node
            """,
            {"tenant_id": str(context.tenant_id), "ids": [str(item) for item in ids]},
        )
        return [FalkorDBMapper.node(row["node"], context.tenant_id) for row in rows]

    async def get_edges(
        self,
        ids: Sequence[RelationshipId],
        context: StorageOperationContext,
    ) -> list[GraphEdge]:
        rows = await self._read(
            """
            MATCH ()-[r]->()
            WHERE r.tenant_id = $tenant_id AND r.id IN $ids
            RETURN r AS edge
            """,
            {"tenant_id": str(context.tenant_id), "ids": [str(item) for item in ids]},
        )
        return [FalkorDBMapper.edge(row["edge"], context.tenant_id) for row in rows]

    async def expand(
        self,
        query: GraphExpansionQuery,
        context: StorageOperationContext,
    ) -> GraphSubgraph:
        selector = self._relationship_selector(query.relationship_types)
        left, right = GraphTraversalSyntax.arrows(query.direction)
        path_limit = query.max_nodes * 4
        rows = await self._read(
            f"""
            MATCH p=(start:HarborEntity){left}[r{selector}*1..{query.max_depth}]
                  {right}(node:HarborEntity)
            WHERE start.tenant_id = $tenant_id
              AND start.id IN $start_ids
              AND all(n IN nodes(p) WHERE n.tenant_id = $tenant_id)
            RETURN nodes(p) AS path_nodes, relationships(p) AS path_edges
            LIMIT $path_limit
            """,
            {
                "tenant_id": str(context.tenant_id),
                "start_ids": [str(item) for item in query.start_nodes],
                "path_limit": path_limit + 1,
            },
        )
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        truncated = len(rows) > path_limit
        for row in rows[:path_limit]:
            for raw_node in row["path_nodes"]:
                node = FalkorDBMapper.node(raw_node, context.tenant_id)
                nodes[str(node.id)] = node
                if len(nodes) >= query.max_nodes:
                    truncated = True
                    break
            for raw_edge in row["path_edges"]:
                edge = FalkorDBMapper.edge(raw_edge, context.tenant_id)
                edges[str(edge.id)] = edge
            if truncated:
                break
        return GraphSubgraph(
            nodes=list(nodes.values()),
            edges=[
                edge
                for edge in edges.values()
                if str(edge.source_id) in nodes and str(edge.target_id) in nodes
            ],
            truncated=truncated,
        )

    async def _read(
        self,
        statement: str,
        parameters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        return FalkorDBMapper.rows(await self._database.read(statement, parameters))

    @staticmethod
    def _relationship_selector(relationship_types: Sequence[str]) -> str:
        if not relationship_types:
            return ""
        return ":" + "|".join(FalkorDBMapper.safe_identifier(item) for item in relationship_types)
