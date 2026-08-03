"""Projection writes and read-back verification for the FalkorDB knowledge graph."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.knowledge_mapping import (
    build_graph_verification,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (
    NODE_LABELS,
    RELATION_IDENTIFIERS,
    read_rows,
)
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphProjectionVerification,
    reject_runtime_fields,
)
from harborrag_core.storage import StorageOperationContext


async def upsert_nodes(
    database: FalkorDBClient,
    nodes: Sequence[GraphNodeRecord],
    *,
    context: StorageOperationContext,
) -> None:
    """Merge node rows by node_key, grouped so each label is one round trip."""

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        row = _node_row(node, tenant_id=str(context.tenant_id))
        reject_runtime_fields(row)
        grouped[NODE_LABELS[node.node_kind]].append(row)
    for label, rows in sorted(grouped.items()):
        await database.write(
            f"""
            UNWIND $rows AS row
            MERGE (node:KnowledgeNode:{label} {{node_key: row.node_key}})
            SET node += row
            """,
            {"rows": rows},
        )


async def upsert_relations(
    database: FalkorDBClient,
    relations: Sequence[GraphEdgeRecord],
    *,
    context: StorageOperationContext,
) -> None:
    """Merge relation rows by relation_id, grouped so each type is one round trip."""

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in relations:
        row = _relation_row(relation, tenant_id=str(context.tenant_id))
        reject_runtime_fields(row)
        grouped[RELATION_IDENTIFIERS[relation.relation_type]].append(
            FalkorDBMapper.encode_properties(row)
        )
    for relationship_type, rows in sorted(grouped.items()):
        await database.write(
            f"""
            UNWIND $rows AS row
            MATCH (source:KnowledgeNode {{node_key: row.source_node_key}})
            MATCH (target:KnowledgeNode {{node_key: row.target_node_key}})
            MERGE (source)-[relation:{relationship_type} {{
                relation_id: row.relation_id
            }}]->(target)
            SET relation += row
            """,
            {"rows": rows},
        )


async def verify_projection(
    database: FalkorDBClient,
    nodes: Sequence[GraphNodeRecord],
    relations: Sequence[GraphEdgeRecord],
    *,
    available_chunk_ids: Sequence[str],
    context: StorageOperationContext,
) -> GraphProjectionVerification:
    """Read the staged projection back and compare it with its manifest."""

    node_keys = tuple(node.node_key for node in nodes)
    relation_ids = tuple(relation.relation_id for relation in relations)
    node_rows = await read_rows(
        database,
        """
        MATCH (node:KnowledgeNode)
        WHERE node.tenant_id = $tenant_id AND node.node_key IN $node_keys
        RETURN node.node_key AS node_key, count(node) AS occurrences
        """,
        {"tenant_id": str(context.tenant_id), "node_keys": list(node_keys)},
    )
    relation_rows = await read_rows(
        database,
        """
        MATCH (source:KnowledgeNode)-[relation]->(target:KnowledgeNode)
        WHERE relation.tenant_id = $tenant_id
          AND relation.relation_id IN $relation_ids
        RETURN relation.relation_id AS relation_id,
               source.node_key AS source_node_key,
               target.node_key AS target_node_key,
               relation.evidence_chunk_ids AS evidence_chunk_ids,
               count(relation) AS occurrences
        """,
        {"tenant_id": str(context.tenant_id), "relation_ids": list(relation_ids)},
    )
    return build_graph_verification(
        node_keys=node_keys,
        relation_ids=relation_ids,
        node_rows=node_rows,
        relation_rows=relation_rows,
        available_chunk_ids=frozenset(available_chunk_ids),
    )


def _node_row(node: GraphNodeRecord, *, tenant_id: str) -> dict[str, Any]:
    return {
        "node_key": node.node_key,
        "logical_id": node.logical_id,
        "document_id": str(node.document_id),
        "document_version_id": str(node.document_version_id),
        "node_kind": node.node_kind.value,
        "title": node.title,
        "connector_type": (node.connector_type.value if node.connector_type is not None else None),
        "document_kind": (node.document_kind.value if node.document_kind is not None else None),
        "source_item_id": node.source_item_id,
        "source_uri": node.source_uri,
        "content_preview": node.content_preview,
        "section_path": list(node.section_path),
        "source_scope_id": node.source_scope_id,
        "tenant_id": tenant_id,
    }


def _relation_row(relation: GraphEdgeRecord, *, tenant_id: str) -> dict[str, Any]:
    return {
        "relation_id": relation.relation_id,
        "relation_type": relation.relation_type.value,
        "source_node_key": relation.source_node_key,
        "target_node_key": relation.target_node_key,
        "document_version_id": str(relation.document_version_id),
        "source_relation_version": relation.source_relation_version,
        "source_explicit": relation.source_explicit,
        "evidence_chunk_ids": list(relation.evidence_chunk_ids),
        "tenant_id": tenant_id,
    }
