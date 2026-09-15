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


async def retire_legacy_source_relations(
    database: FalkorDBClient,
    source_scope_id: str,
    nodes: Sequence[GraphNodeRecord],
    relations: Sequence[GraphEdgeRecord],
    *,
    context: StorageOperationContext,
) -> None:
    """Finish a scoped rebuild; the caller supplies the complete active manifest set.

    Only legacy unowned assertions are removed. Canonical artifacts, modern
    supports, stable identities and other scopes are untouched. Call after
    reindex and source repair, with the scope's document publication quiesced.
    """

    if not source_scope_id.strip() or not nodes or not relations:
        raise ValueError("legacy retirement requires a named scope and rebuilt manifests")
    if any(
        relation.source_scope_id != source_scope_id
        or str(relation.owner_id) != str(context.tenant_id)
        or (
            relation.relation_type.value != "has_data_source"
            and relation.document_version_id is None
        )
        for relation in relations
    ):
        raise ValueError("legacy retirement requires modern supports from the named tenant/scope")
    verification = await knowledge_writes.verify_projection(
        database, nodes, relations, context=context
    )
    if not verification.valid:
        raise ValueError("rebuilt source manifests failed verification")
    await database.write(
        """
        MATCH ()-[relation]->()
        WHERE relation.tenant_id = $tenant_id
          AND relation.graph_schema_version = $graph_schema_version
          AND relation.source_scope_id = $source_scope_id
          AND relation.ownership_scope = 'SOURCE_SCOPE'
          AND relation.relation_type <> 'has_data_source'
        DELETE relation
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "source_scope_id": source_scope_id,
        },
    )
