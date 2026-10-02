"""Tenant-scoped deletion and inventory for the FalkorDB knowledge graph."""

from __future__ import annotations

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import read_rows
from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION
from harborrag_core.storage import StorageOperationContext

_COUNT_STATEMENTS = (
    """
    MATCH (node:KnowledgeNode)
    WHERE node.tenant_id = $tenant_id
      AND node.graph_schema_version = $graph_schema_version
    RETURN count(node) AS item_count
    """,
    """
    MATCH ()-[relation]->()
    WHERE relation.tenant_id = $tenant_id
      AND relation.graph_schema_version = $graph_schema_version
    RETURN count(relation) AS item_count
    """,
)


async def delete_version(
    database: FalkorDBClient,
    document_version_id: str,
    *,
    context: StorageOperationContext,
) -> None:
    """Idempotently remove a retired or failed version projection."""

    await _delete_projection(
        database,
        version_scoped=True,
        parameters={
            "tenant_id": str(context.tenant_id),
            "document_version_id": document_version_id,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
        },
    )
    # Other projections stage shared nodes before edges. Online orphan pruning
    # would race those writes, even for nodes this version previously supported.


async def delete_source_item(
    database: FalkorDBClient,
    source_item_node_key: str,
    *,
    context: StorageOperationContext,
) -> None:
    """Remove one stable source item and its version-owned descendants."""

    if not source_item_node_key.strip():
        raise ValueError("source item node key must be non-empty")
    parameters = {
        "tenant_id": str(context.tenant_id),
        "node_key": source_item_node_key,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
    }
    await database.write(
        """
        MATCH (item:KnowledgeNode {node_key: $node_key})
        WHERE item.tenant_id = $tenant_id
          AND item.graph_schema_version = $graph_schema_version
          AND item.ownership_scope = 'SOURCE_SCOPE'
        OPTIONAL MATCH (item)-[:HAS_VERSION]->(version:KnowledgeNode)
        WHERE version.graph_schema_version = $graph_schema_version
        WITH item, collect(version.document_version_id) AS version_ids
        OPTIONAL MATCH ()-[support]->()
        WHERE support.tenant_id = $tenant_id
          AND support.graph_schema_version = $graph_schema_version
          AND support.ownership_scope = 'DOCUMENT_VERSION'
          AND support.document_version_id IN version_ids
        DELETE support
        WITH DISTINCT item, version_ids
        OPTIONAL MATCH (owned:KnowledgeNode)
        WHERE owned.tenant_id = $tenant_id
          AND owned.graph_schema_version = $graph_schema_version
          AND owned.ownership_scope = 'DOCUMENT_VERSION'
          AND owned.document_version_id IN version_ids
        DETACH DELETE owned, item
        """,
        parameters,
    )


async def delete_source_scope(
    database: FalkorDBClient,
    source_scope_id: str,
    *,
    context: StorageOperationContext,
) -> None:
    """Remove one connection without affecting the tenant or other connections."""

    if not source_scope_id.strip():
        raise ValueError("source_scope_id must be non-empty")
    parameters = {
        "tenant_id": str(context.tenant_id),
        "source_scope_id": source_scope_id,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
    }
    await database.write(
        """
        MATCH ()-[relation]->()
        WHERE relation.tenant_id = $tenant_id
          AND relation.graph_schema_version = $graph_schema_version
          AND relation.source_scope_id = $source_scope_id
        DELETE relation
        """,
        parameters,
    )
    await database.write(
        """
        MATCH (node:KnowledgeNode)
        WHERE node.tenant_id = $tenant_id
          AND node.graph_schema_version = $graph_schema_version
          AND node.source_scope_id = $source_scope_id
        DETACH DELETE node
        """,
        parameters,
    )


async def delete_tenant_projection(
    database: FalkorDBClient,
    *,
    context: StorageOperationContext,
) -> None:
    """Delete only one tenant's rebuildable projection from the shared graph."""

    await _delete_projection(
        database,
        version_scoped=False,
        parameters={
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
        },
    )


async def tenant_projection_counts(
    database: FalkorDBClient,
    *,
    context: StorageOperationContext,
) -> tuple[int, int]:
    """Return tenant-scoped node and relationship counts for administration."""

    parameters = {
        "tenant_id": str(context.tenant_id),
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
    }
    counts = [await read_rows(database, statement, parameters) for statement in _COUNT_STATEMENTS]
    return tuple(int(rows[0]["item_count"]) if rows else 0 for rows in counts)  # type: ignore[return-value]


async def _delete_projection(
    database: FalkorDBClient,
    *,
    version_scoped: bool,
    parameters: dict[str, str],
) -> None:
    """Delete relations before nodes so no relation outlives its endpoints."""

    version_filter = " AND {alias}.document_version_id = $document_version_id"
    for pattern, alias, deletion in (
        ("()-[relation]->()", "relation", "DELETE relation"),
        ("(node:KnowledgeNode)", "node", "DETACH DELETE node"),
    ):
        predicate = (
            f"{alias}.tenant_id = $tenant_id "
            f"AND {alias}.graph_schema_version = $graph_schema_version"
        )
        if version_scoped:
            predicate += f" AND {alias}.ownership_scope = 'DOCUMENT_VERSION'"
            predicate += version_filter.format(alias=alias)
        await database.write(
            f"MATCH {pattern} WHERE {predicate} {deletion}",
            parameters,
        )
