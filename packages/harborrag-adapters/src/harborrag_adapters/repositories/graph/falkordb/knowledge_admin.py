"""Tenant-scoped deletion and inventory for the FalkorDB knowledge graph."""

from __future__ import annotations

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import read_rows
from harborrag_core.storage import StorageOperationContext

_COUNT_STATEMENTS = (
    """
    MATCH (node:KnowledgeNode)
    WHERE node.tenant_id = $tenant_id
    RETURN count(node) AS item_count
    """,
    """
    MATCH ()-[relation]->()
    WHERE relation.tenant_id = $tenant_id
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
        },
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
        parameters={"tenant_id": str(context.tenant_id)},
    )


async def tenant_projection_counts(
    database: FalkorDBClient,
    *,
    context: StorageOperationContext,
) -> tuple[int, int]:
    """Return tenant-scoped node and relationship counts for administration."""

    parameters = {"tenant_id": str(context.tenant_id)}
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
        predicate = f"{alias}.tenant_id = $tenant_id"
        if version_scoped:
            predicate += version_filter.format(alias=alias)
        await database.write(
            f"MATCH {pattern} WHERE {predicate} {deletion}",
            parameters,
        )
