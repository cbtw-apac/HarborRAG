"""FalkorDB knowledge-graph repository.

The port surface lives here; the Cypher and the row handling live in focused sibling
modules (`knowledge_provisioning`, `knowledge_writes`, `knowledge_queries`,
`knowledge_admin`) so each has a single reason to change.
"""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_adapters.repositories.graph.falkordb import (
    knowledge_admin,
    knowledge_node_resolution,
    knowledge_provisioning,
    knowledge_queries,
    knowledge_repair,
    knowledge_writes,
)
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphProjectionVerification,
    GraphSchemaMigrationVerification,
    KnowledgeGraphTraversal,
)
from harborrag_core.retrieval import (
    GraphNodeResolutionQuery,
    GraphNodeResolutionResult,
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext

from .tenant_pool import GraphClientFactory, TenantGraphClientPool


class FalkorKnowledgeGraphRepository:
    """Persist the rebuildable, version-addressed HarborRAG knowledge graph."""

    def __init__(
        self,
        config: FalkorDBGraphConfig,
        *,
        client: FalkorDBClient | None = None,
        client_factory: GraphClientFactory | None = None,
    ) -> None:
        self._config = config
        self._pool = TenantGraphClientPool(
            config,
            client=client,
            factory=client_factory,
            provisioner=knowledge_provisioning.provision_graph,
        )

    async def connect(self, *, provision: bool = True) -> None:
        await self._pool.connect(provision=provision)

    async def close(self) -> None:
        await self._pool.close()

    async def provision(self, context: StorageOperationContext | None = None) -> None:
        await self._pool.provision(context)

    async def database_for(
        self, context: StorageOperationContext, *, write: bool = False
    ) -> FalkorDBClient:
        return await self._pool.database_for(context, write=write)

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
        await knowledge_writes.upsert_nodes(
            await self.database_for(context, write=True), nodes, context=context
        )

    async def upsert_relations(
        self,
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_writes.upsert_relations(
            await self.database_for(context, write=True), relations, context=context
        )

    async def verify_projection(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> GraphProjectionVerification:
        return await knowledge_writes.verify_projection(
            await self.database_for(context),
            nodes,
            relations,
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
            await self.database_for(context),
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
        return await knowledge_queries.search_triplets(
            await self.database_for(context), query, context=context
        )

    async def find_paths(
        self,
        query: GraphPathQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphPathResult:
        return await knowledge_queries.find_paths(
            await self.database_for(context), query, context=context
        )

    async def expand_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal:
        return await knowledge_queries.expand_subgraph(
            await self.database_for(context), query, context=context
        )

    async def resolve_nodes(
        self,
        query: GraphNodeResolutionQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphNodeResolutionResult:
        return await knowledge_node_resolution.resolve_knowledge_nodes(
            await self.database_for(context), query, context=context
        )

    async def delete_relations(
        self,
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Retract relations, leaving their now-edgeless far ends for cleanup to reap."""

        if not relations:
            return
        await knowledge_writes.delete_relations(
            await self.database_for(context, write=True), relations, context=context
        )

    async def delete_version(
        self,
        document_version_id: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_admin.delete_version(
            await self.database_for(context, write=True),
            document_version_id,
            context=context,
        )

    async def replace_source_relations(
        self,
        document_version_id: str,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_repair.replace_source_relations(
            await self.database_for(context, write=True),
            document_version_id,
            nodes,
            relations,
            context=context,
        )

    async def retire_legacy_source_relations(
        self,
        source_scope_id: str,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_repair.retire_legacy_source_relations(
            await self.database_for(context, write=True),
            source_scope_id,
            nodes,
            relations,
            context=context,
        )

    async def delete_source_item(
        self,
        source_item_node_key: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_admin.delete_source_item(
            await self.database_for(context, write=True),
            source_item_node_key,
            context=context,
        )

    async def delete_source_scope(
        self,
        source_scope_id: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_admin.delete_source_scope(
            await self.database_for(context, write=True),
            source_scope_id,
            context=context,
        )

    async def verify_schema_v2_migration(
        self,
        *,
        evidence_chunk_ids: Sequence[str],
        active_source_item_node_keys: Sequence[str],
        context: StorageOperationContext,
    ) -> GraphSchemaMigrationVerification:
        return await knowledge_admin.verify_schema_v2_migration(
            await self.database_for(context),
            evidence_chunk_ids=tuple(evidence_chunk_ids),
            active_source_item_node_keys=tuple(active_source_item_node_keys),
            context=context,
        )

    async def delete_legacy_tenant_projection(
        self,
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_admin.delete_legacy_tenant_projection(
            await self.database_for(context, write=True),
            context=context,
        )

    async def tenant_projection_counts(
        self,
        *,
        context: StorageOperationContext,
    ) -> tuple[int, int]:
        return await knowledge_admin.tenant_projection_counts(
            await self.database_for(context), context=context
        )

    async def delete_tenant_projection(
        self,
        *,
        context: StorageOperationContext,
    ) -> None:
        await knowledge_admin.delete_tenant_projection(
            await self.database_for(context, write=True), context=context
        )
