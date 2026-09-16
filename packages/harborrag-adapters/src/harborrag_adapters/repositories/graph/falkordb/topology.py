"""Build-scoped semantic projection, invisible to structural graph query contracts."""

from __future__ import annotations

from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.derived import ParentDescription
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.records import DocumentTopologyBuild

from .client import FalkorDBClient
from .config import FalkorDBGraphConfig
from .knowledge_provisioning import is_already_exists_error
from .knowledge_support import read_rows
from .parent_views import ParentViewProjection
from .tenant_pool import GraphClientFactory, TenantGraphClientPool
from .topology_mapping import projection_rows
from .typed_topology import TypedTopologyProjection
from .unified_topology import UnifiedTopologyProjection


class FalkorTopologyRepository:
    def __init__(
        self,
        config: FalkorDBGraphConfig,
        *,
        client: FalkorDBClient | None = None,
        client_factory: GraphClientFactory | None = None,
    ):
        self._pool = TenantGraphClientPool(
            config,
            client=client,
            factory=client_factory,
            provisioner=self._provision,
        )

    async def connect(self, *, provision: bool = True) -> None:
        await self._pool.connect(provision=provision)

    @staticmethod
    async def _provision(database: FalkorDBClient) -> None:
        await FalkorTopologyRepository._provision_label(database, "Entity", "id")

    @staticmethod
    async def _provision_label(database: FalkorDBClient, label: str, identity: str) -> None:
        for name in ("tenant_id", "build_id", identity):
            try:
                await database.write(f"CREATE INDEX FOR (n:{label}) ON (n.{name})", {})
            except Exception as error:
                if not is_already_exists_error(error):
                    raise
        try:
            await database.create_unique_node_constraint(
                label=label, properties=("tenant_id", "build_id", identity)
            )
        except Exception as error:
            if not is_already_exists_error(error):
                raise

    async def close(self) -> None:
        await self._pool.close()

    async def database_for(
        self, context: StorageOperationContext, *, write: bool = False
    ) -> FalkorDBClient:
        return await self._pool.database_for(context, write=write)

    async def write(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> None:
        if build.projection_revision in {
            "semantic-v3",
            "semantic-v4",
            "semantic-v5",
            "semantic-v6",
        }:
            unified_projection = UnifiedTopologyProjection(
                await self.database_for(context, write=True)
            )
            await unified_projection.write(build, context=context)
            return
        if build.projection_revision == "semantic-v2":
            typed_projection = TypedTopologyProjection(await self.database_for(context, write=True))
            await typed_projection.write(build, context=context)
            return
        if build.projection_revision != "semantic-v1":
            raise ValueError("unsupported semantic projection revision")
        nodes, edges = projection_rows(build, str(context.tenant_id))
        database = await self.database_for(context, write=True)
        for start in range(0, len(nodes), 250):
            await database.write(
                "UNWIND $rows AS row "
                "MERGE (n:TopologyRecord {tenant_id: row.tenant_id, "
                "build_id: row.build_id, record_key: row.record_key}) SET n = row",
                {"rows": nodes[start : start + 250]},
            )
        for start in range(0, len(edges), 250):
            await database.write(
                "UNWIND $rows AS row "
                "MATCH (s:TopologyRecord {tenant_id: row.tenant_id, "
                "build_id: row.build_id, record_key: row.source}), "
                "(t:TopologyRecord {tenant_id: row.tenant_id, "
                "build_id: row.build_id, record_key: row.target}) "
                "MERGE (s)-[r:TOPOLOGY_LINK {tenant_id: row.tenant_id, "
                "build_id: row.build_id, relation_key: row.relation_key}]->(t) "
                "SET r.role = row.role",
                {"rows": edges[start : start + 250]},
            )

    async def verify(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> bool:
        if build.projection_revision in {
            "semantic-v3",
            "semantic-v4",
            "semantic-v5",
            "semantic-v6",
        }:
            unified_projection = UnifiedTopologyProjection(await self.database_for(context))
            return await unified_projection.verify(build, context=context)
        if build.projection_revision == "semantic-v2":
            typed_projection = TypedTopologyProjection(await self.database_for(context))
            return await typed_projection.verify(build, context=context)
        if build.projection_revision != "semantic-v1":
            raise ValueError("unsupported semantic projection revision")
        nodes, edges = projection_rows(build, str(context.tenant_id))
        params = {"tenant_id": str(context.tenant_id), "build_id": build.build_id}
        database = await self.database_for(context)
        actual_nodes = await read_rows(
            database,
            "MATCH (n:TopologyRecord {tenant_id: $tenant_id, build_id: $build_id}) "
            "RETURN properties(n) AS properties",
            params,
        )
        actual_edges = await read_rows(
            database,
            "MATCH (s)"
            "-[r:TOPOLOGY_LINK {tenant_id: $tenant_id, build_id: $build_id}]->"
            "(t) "
            "RETURN properties(r) AS properties, s.record_key AS source, "
            "t.record_key AS target, s.tenant_id AS source_tenant, "
            "s.build_id AS source_build, t.tenant_id AS target_tenant, "
            "t.build_id AS target_build",
            params,
        )
        expected_edges = [
            {
                "properties": {
                    key: value for key, value in row.items() if key not in {"source", "target"}
                },
                "source": row["source"],
                "target": row["target"],
                "source_tenant": str(context.tenant_id),
                "target_tenant": str(context.tenant_id),
                "source_build": build.build_id,
                "target_build": build.build_id,
            }
            for row in edges
        ]
        try:
            return (
                len(actual_nodes) == len(nodes)
                and len(actual_edges) == len(edges)
                and {digest(row["properties"]) for row in actual_nodes}
                == {digest(row) for row in nodes}
                and {digest(row) for row in actual_edges} == {digest(row) for row in expected_edges}
            )
        except (KeyError, TypeError, ValueError):
            return False

    async def delete_build(self, build_id: str, *, context: StorageOperationContext) -> None:
        if not build_id.strip():
            raise ValueError("build deletion requires a non-empty build ID")
        database = await self.database_for(context, write=True)
        await database.write(
            "MATCH (n:KnowledgeNode:Chunk {tenant_id: $tenant_id, "
            "description_build_id: $build_id}) "
            "REMOVE n.description_build_id, n.title, n.title_key, n.generated_title, "
            "n.generated_description, n.retrieval_context "
            "SET n.name = 'Chunk', n.description = 'Document evidence chunk.'",
            {"tenant_id": str(context.tenant_id), "build_id": build_id},
        )
        await database.write(
            "MATCH (n:KnowledgeNode {tenant_id: $tenant_id, "
            "description_build_id: $build_id}) WHERE NOT n:Chunk "
            "REMOVE n.description_build_id, n.generated_description "
            "SET n.description = CASE "
            "WHEN n:Structure AND n.title IS NOT NULL "
            "THEN 'Document ' + n.entity_type + ': ' + n.title + '.' "
            "WHEN n:DocumentVersion THEN 'Document Version in the source topology.' "
            "ELSE n.entity_type + ' in the source topology.' END",
            {"tenant_id": str(context.tenant_id), "build_id": build_id},
        )
        await database.write(
            "MATCH (n {tenant_id: $tenant_id, build_id: $build_id}) "
            "WHERE n:TopologyRecord OR n:TopologyParentView DETACH DELETE n",
            {"tenant_id": str(context.tenant_id), "build_id": build_id},
        )

    async def write_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> None:
        projection = ParentViewProjection(await self.database_for(context, write=True))
        await projection.write(build, parents, context=context)

    async def verify_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> bool:
        projection = ParentViewProjection(await self.database_for(context))
        return await projection.verify(build, parents, context=context)
