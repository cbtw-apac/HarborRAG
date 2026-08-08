from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_adapters.repositories.errors import (
    HarborUnsafeQueryError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.graph.base import HarborGraphRepository
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.knowledge_provisioning import (
    is_already_exists_error,
)
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_adapters.repositories.graph.falkordb.reader import FalkorDBGraphReader
from harborrag_adapters.repositories.policies.tenancy import ensure_tenant
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)
from harborrag_core.schemas.graph import (
    GraphEdge,
    GraphExpansionQuery,
    GraphNode,
    GraphStoreCapabilities,
    GraphSubgraph,
)
from harborrag_core.schemas.ids import EntityId, RelationshipId
from harborrag_core.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)


class FalkorDBGraphRepository(HarborGraphRepository):
    """Persists tenant-scoped graph records through FalkorDB openCypher queries."""

    def __init__(
        self,
        config: FalkorDBGraphConfig,
        telemetry: StorageTelemetryHook | None = None,
        *,
        client: FalkorDBClient | None = None,
    ) -> None:
        self._config = config
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.GRAPH,
            backend="falkordb",
        )
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
        self._reader = FalkorDBGraphReader(self._database)

    @property
    def capabilities(self) -> GraphStoreCapabilities:
        """Describe the portable FalkorDB features exposed by this repository."""
        return GraphStoreCapabilities(
            transactions=False,
            shortest_path=False,
            bounded_expansion=True,
            temporal_properties=True,
            constraints=False,
            indexes=True,
            advanced_native_query=False,
        )

    async def connect(self) -> None:
        await self._database.connect()
        await self._initialize_schema()

    async def close(self) -> None:
        await self._database.close()

    async def health(self) -> RepositoryHealth:
        try:
            await self._database.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - live provider behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.GRAPH,
            backend="falkordb",
            instance_name=self._config.instance_name,
            status=status,
            details=details,
        )

    @traced_repository_operation("upsert_nodes")
    async def upsert_nodes(
        self,
        nodes: Sequence[GraphNode],
        *,
        context: StorageOperationContext,
    ) -> None:
        rows: list[dict[str, Any]] = []
        for node in nodes:
            ensure_tenant(
                node.tenant_id,
                context,
                error_context=self._error_context("upsert_nodes", context),
            )
            rows.append(
                {
                    "id": str(node.id),
                    "tenant_id": str(context.tenant_id),
                    "labels": sorted(node.labels),
                    "properties": FalkorDBMapper.encode_properties(
                        {
                            **node.properties,
                            "valid_from": node.valid_from.isoformat(),
                            "valid_to": (node.valid_to.isoformat() if node.valid_to else None),
                            "confidence": node.confidence,
                            "provenance": node.provenance,
                        }
                    ),
                }
            )
        await self._write(
            """
            UNWIND $rows AS row
            MERGE (n:HarborEntity {tenant_id: row.tenant_id, id: row.id})
            SET n.labels = row.labels
            SET n += row.properties
            """,
            {"rows": rows},
        )

    @traced_repository_operation("upsert_edges")
    async def upsert_edges(
        self,
        edges: Sequence[GraphEdge],
        *,
        context: StorageOperationContext,
    ) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            ensure_tenant(
                edge.tenant_id,
                context,
                error_context=self._error_context("upsert_edges", context),
            )
            relationship_type = FalkorDBMapper.safe_identifier(edge.relationship_type)
            grouped.setdefault(relationship_type, []).append(
                {
                    "id": str(edge.id),
                    "tenant_id": str(context.tenant_id),
                    "source_id": str(edge.source_id),
                    "target_id": str(edge.target_id),
                    "properties": FalkorDBMapper.encode_properties(
                        {
                            **edge.properties,
                            "source_id": str(edge.source_id),
                            "target_id": str(edge.target_id),
                            "valid_from": edge.valid_from.isoformat(),
                            "valid_to": (edge.valid_to.isoformat() if edge.valid_to else None),
                            "confidence": edge.confidence,
                            "provenance": edge.provenance,
                        }
                    ),
                }
            )
        for relationship_type, rows in grouped.items():
            await self._write(
                f"""
                UNWIND $rows AS row
                MATCH (source:HarborEntity {{tenant_id: row.tenant_id, id: row.source_id}})
                MATCH (target:HarborEntity {{tenant_id: row.tenant_id, id: row.target_id}})
                MERGE (source)-[r:{relationship_type} {{
                    tenant_id: row.tenant_id, id: row.id
                }}]->(target)
                SET r += row.properties
                """,
                {"rows": rows},
            )

    @traced_repository_operation("get_nodes")
    async def get_nodes(
        self,
        ids: Sequence[EntityId],
        *,
        context: StorageOperationContext,
    ) -> list[GraphNode]:
        return await self._reader.get_nodes(ids, context)

    @traced_repository_operation("get_edges")
    async def get_edges(
        self,
        ids: Sequence[RelationshipId],
        *,
        context: StorageOperationContext,
    ) -> list[GraphEdge]:
        return await self._reader.get_edges(ids, context)

    @traced_repository_operation("delete_nodes")
    async def delete_nodes(
        self,
        ids: Sequence[EntityId],
        *,
        context: StorageOperationContext,
    ) -> None:
        await self._write(
            """
            MATCH (n:HarborEntity)
            WHERE n.tenant_id = $tenant_id AND n.id IN $ids
            DETACH DELETE n
            """,
            {"tenant_id": str(context.tenant_id), "ids": [str(item) for item in ids]},
        )

    @traced_repository_operation("delete_edges")
    async def delete_edges(
        self,
        ids: Sequence[RelationshipId],
        *,
        context: StorageOperationContext,
    ) -> None:
        await self._write(
            """
            MATCH ()-[r]->()
            WHERE r.tenant_id = $tenant_id AND r.id IN $ids
            DELETE r
            """,
            {"tenant_id": str(context.tenant_id), "ids": [str(item) for item in ids]},
        )

    @traced_repository_operation("expand")
    async def expand(
        self,
        query: GraphExpansionQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphSubgraph:
        return await self._reader.expand(query, context)

    @traced_repository_operation("advanced_query")
    async def advanced_query(
        self,
        statement: str,
        parameters: Mapping[str, Any],
        *,
        context: StorageOperationContext,
    ) -> list[dict[str, Any]]:
        del statement, parameters
        raise HarborUnsafeQueryError(
            "advanced FalkorDB queries are unavailable because arbitrary Cypher "
            "cannot guarantee tenant isolation; use the typed graph operations",
            context=self._error_context("advanced_query", context),
        )

    async def _initialize_schema(self) -> None:
        for statement in (
            "CREATE INDEX FOR (n:HarborEntity) ON (n.tenant_id)",
            "CREATE INDEX FOR (n:HarborEntity) ON (n.id)",
            "CREATE INDEX FOR (n:HarborEntity) ON (n.artifact_id)",
            "CREATE INDEX FOR (n:HarborEntity) ON (n.is_active)",
        ):
            try:
                await self._write(statement, {})
            except Exception as exc:  # pragma: no cover - provider version behavior
                if not is_already_exists_error(exc):
                    raise

    async def _write(self, statement: str, parameters: Mapping[str, Any]) -> None:
        await self._database.write(statement, parameters)

    def _error_context(
        self,
        operation: str,
        context: StorageOperationContext,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.GRAPH,
            backend="falkordb",
            instance_name=self._config.instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
        )
