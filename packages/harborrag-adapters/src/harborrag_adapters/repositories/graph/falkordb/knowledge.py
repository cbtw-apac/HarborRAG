"""FalkorDB knowledge-graph repository.

The port surface lives here; the Cypher and the row handling live in focused sibling
modules (`knowledge_provisioning`, `knowledge_writes`, `knowledge_queries`,
`knowledge_admin`) so each has a single reason to change.
"""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_adapters.repositories.graph.falkordb import (
    knowledge_admin,
    knowledge_provisioning,
    knowledge_queries,
    knowledge_writes,
)
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphProjectionVerification,
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


class FalkorKnowledgeGraphRepository:
    """Persist the rebuildable, version-addressed HarborRAG knowledge graph."""

    def __init__(
        self,
        config: FalkorDBGraphConfig,
        *,
        client: FalkorDBClient | None = None,
    ) -> None:
        self._config = config
        self._database = client or FalkorDBClient(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password.get_secret_value() if config.password else None,
            graph_name=config.graph_name,
            ssl=config.ssl,
            max_connections=config.max_connections,
            connect_timeout_seconds=config.connect_timeout_seconds,
            operation_timeout_seconds=config.operation_timeout_seconds,
        )

    async def connect(self) -> None:
        await self._database.connect()
        await self.provision()

    async def close(self) -> None:
        await self._database.close()

    async def provision(self) -> None:
        await knowledge_provisioning.provision_graph(self._database)

    async def write_projection(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Idempotently stage nodes before their parameterized relations."""

        await self.upsert_nodes(nodes, context=context)
        await self.upsert_relations(relations, context=context)

    async def upsert_nodes(
        self,
        nodes: Sequence[GraphNodeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_writes.upsert_nodes(self._database, nodes, context=context)

    async def upsert_relations(
        self,
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_writes.upsert_relations(self._database, relations, context=context)

    async def verify_projection(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        available_chunk_ids: Sequence[str],
        context: StorageOperationContext,
    ) -> GraphProjectionVerification:
        return await knowledge_writes.verify_projection(
            self._database,
            nodes,
            relations,
            available_chunk_ids=available_chunk_ids,
            context=context,
        )

    async def traverse(
        self,
        start_node_key: str,
        *,
        max_depth: int,
        max_nodes: int,
        direction: str,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal:
        return await knowledge_queries.traverse(
            self._database,
            start_node_key,
            bounds=knowledge_queries.TraversalBounds(
                max_depth=max_depth,
                max_nodes=max_nodes,
                direction=direction,
            ),
            context=context,
        )

    async def search_triplets(
        self,
        query: GraphTripletQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphTripletResult:
        return await knowledge_queries.search_triplets(self._database, query, context=context)

    async def find_paths(
        self,
        query: GraphPathQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphPathResult:
        return await knowledge_queries.find_paths(self._database, query, context=context)

    async def expand_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal:
        return await knowledge_queries.expand_subgraph(self._database, query, context=context)

    async def delete_version(
        self,
        document_version_id: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_admin.delete_version(
            self._database,
            document_version_id,
            context=context,
        )

    async def tenant_projection_counts(
        self,
        *,
        context: StorageOperationContext,
    ) -> tuple[int, int]:
        return await knowledge_admin.tenant_projection_counts(self._database, context=context)

    async def delete_tenant_projection(
        self,
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_admin.delete_tenant_projection(self._database, context=context)
