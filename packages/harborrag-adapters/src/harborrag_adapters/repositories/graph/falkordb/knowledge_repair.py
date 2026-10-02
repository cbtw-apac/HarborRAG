"""Reconcile native link supports without guessing previous endpoint identities."""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION, GraphEdgeRecord, GraphNodeRecord
from harborrag_core.storage import StorageOperationContext

from . import knowledge_writes
from .client import FalkorDBClient


async def replace_source_relations(
    database: FalkorDBClient,
    document_version_id: str,
    nodes: Sequence[GraphNodeRecord],
    relations: Sequence[GraphEdgeRecord],
    *,
    context: StorageOperationContext,
) -> None:
    """Upsert/verify before retraction; retries converge and never delete other supports.

    Replacement is eventually consistent across graph statements. A failed write
    leaves prior links available for retry. The serving validator independently
    rejects this entire owner version if publication has since superseded it.
    """

    if not document_version_id.strip():
        raise ValueError("source relation replacement requires a document version")
    if any(
        str(relation.document_version_id) != document_version_id
        or relation.attributes.get("source_relation") is not True
        for relation in relations
    ):
        raise ValueError("replacement must contain only the named version's native link supports")
    await knowledge_writes.upsert_nodes(database, nodes, context=context)
    await knowledge_writes.upsert_relations(database, relations, context=context)
    verification = await knowledge_writes.verify_projection(
        database, nodes, relations, context=context
    )
    if not verification.valid:
        raise ValueError("source relation replacement failed verification")
    await database.write(
        """
        MATCH ()-[relation]->()
        WHERE relation.tenant_id = $tenant_id
          AND relation.graph_schema_version = $graph_schema_version
          AND relation.ownership_scope = 'DOCUMENT_VERSION'
          AND relation.document_version_id = $document_version_id
          AND relation.source_relation = true
          AND NOT relation.relation_id IN $retained_ids
        DELETE relation
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "document_version_id": document_version_id,
            "retained_ids": [relation.relation_id for relation in relations],
        },
    )
