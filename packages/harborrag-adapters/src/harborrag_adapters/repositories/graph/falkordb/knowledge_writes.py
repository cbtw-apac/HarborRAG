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
    GRAPH_SCHEMA_VERSION,
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
    """Merge node rows by tenant, node_key, and schema version, one round trip per label."""

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    placeholders: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        if str(node.owner_id) != str(context.tenant_id):
            raise ValueError("graph node owner does not match storage tenant context")
        row = _node_row(node, tenant_id=str(context.tenant_id))
        reject_runtime_fields(row)
        target = placeholders if node.attributes.get("placeholder") is True else grouped
        target[NODE_LABELS[node.node_kind]].append(FalkorDBMapper.encode_properties(row))
    for label, rows in sorted(grouped.items()):
        # tenant_id is part of the merge identity, not just a filter property. Version-owned
        # node keys (DocumentVersion, Structure, Chunk) do not hash the tenant, so without
        # it two tenants that produced the same document version would share one node.
        # SET node = row replaces the whole property map instead of patching it, so a field
        # dropped from a later projection cannot survive as stale state.
        await database.write(
            f"""
            UNWIND $rows AS row
            MERGE (node:KnowledgeNode:{label} {{
                node_key: row.node_key,
                graph_schema_version: row.graph_schema_version,
                tenant_id: row.tenant_id
            }})
            SET node = row
            """,
            {"rows": rows},
        )
    for label, rows in sorted(placeholders.items()):
        # A placeholder shares the real node's key so the concrete projection can
        # claim it later. It must therefore only ever fill a gap: writing it over
        # an existing node would downgrade concrete provider metadata to a stub.
        # A placeholder overwriting a placeholder is safe and is the only refresh
        # path these nodes have (renamed above-scope ancestors, moved folders).
        await database.write(
            f"""
            UNWIND $rows AS row
            MERGE (node:KnowledgeNode:{label} {{
                node_key: row.node_key,
                graph_schema_version: row.graph_schema_version,
                tenant_id: row.tenant_id
            }})
            ON CREATE SET node = row
            ON MATCH SET node = CASE WHEN node.placeholder = true THEN row ELSE node END
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
        if str(relation.owner_id) != str(context.tenant_id):
            raise ValueError("graph relationship owner does not match storage tenant context")
        row = _relation_row(relation, tenant_id=str(context.tenant_id))
        reject_runtime_fields(row)
        grouped[RELATION_IDENTIFIERS[relation.relation_type]].append(
            FalkorDBMapper.encode_properties(row)
        )
    for relationship_type, rows in sorted(grouped.items()):
        await database.write(
            f"""
            UNWIND $rows AS row
            MATCH (source:KnowledgeNode {{
                node_key: row.source_node_key,
                graph_schema_version: row.graph_schema_version,
                tenant_id: row.tenant_id
            }})
            MATCH (target:KnowledgeNode {{
                node_key: row.target_node_key,
                graph_schema_version: row.graph_schema_version,
                tenant_id: row.tenant_id
            }})
            MERGE (source)-[relation:{relationship_type} {{
                relation_id: row.relation_id
            }}]->(target)
            SET relation = row
            """,
            {"rows": rows},
        )


async def delete_relations(
    database: FalkorDBClient,
    relations: Sequence[GraphEdgeRecord],
    *,
    context: StorageOperationContext,
) -> None:
    """Retract named relations by relation_id, grouped so each type is one round trip.

    Grouping is not only symmetry with ``upsert_relations``: the only relation index
    provisioning creates is per-relationship-label on ``tenant_id``, so an untyped
    ``MATCH ()-[r]->()`` would scan every relationship in the graph. Naming the label
    keeps each statement inside the one set it can use that index on.

    ``relation_id`` is derived from type, endpoints and source relation version, so the
    caller can name an edge it wrote earlier without reading the graph back first.
    """

    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for relation in relations:
        if str(relation.owner_id) != str(context.tenant_id):
            raise ValueError("graph relationship owner does not match storage tenant context")
        grouped[RELATION_IDENTIFIERS[relation.relation_type]].append(relation.relation_id)
    for relationship_type, relation_ids in sorted(grouped.items()):
        await database.write(
            f"""
            MATCH ()-[relation:{relationship_type}]->()
            WHERE relation.tenant_id = $tenant_id
              AND relation.graph_schema_version = $graph_schema_version
              AND relation.relation_id IN $relation_ids
            DELETE relation
            """,
            {
                "tenant_id": str(context.tenant_id),
                "graph_schema_version": GRAPH_SCHEMA_VERSION,
                "relation_ids": relation_ids,
            },
        )
    await _prune_retracted_targets(database, relations, context=context)


async def _prune_retracted_targets(
    database: FalkorDBClient,
    relations: Sequence[GraphEdgeRecord],
    *,
    context: StorageOperationContext,
) -> None:
    """Drop the far ends left with no edges, and only those.

    A retracted relation is normally the last edge of a placeholder stub, and a
    zero-degree stub is not merely untidy: it counts in ``tenant_projection_counts`` and
    appears in the graph-eval health baseline, so leaving it turns one repaired document
    into a permanent diff.

    Scoped to the keys just retracted, not to the tenant. Relation repair fans out with
    ``asyncio.gather``, and ``write_projection`` stages nodes before their relations, so
    another document's freshly written placeholders are momentarily zero-degree. A
    tenant-wide sweep firing in that window deletes them, and that document's own
    ``MATCH source MATCH target MERGE`` then writes no relation at all and fails its
    verification.

    ``NOT (node)--()`` still guards each key, so a stub that some other projection has
    since attached to a container survives.
    """

    await database.write(
        """
        MATCH (node:KnowledgeNode)
        WHERE node.node_key IN $node_keys
          AND node.tenant_id = $tenant_id
          AND node.graph_schema_version = $graph_schema_version
          AND node.ownership_scope = 'SOURCE_SCOPE'
          AND NOT (node)--()
        DELETE node
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "node_keys": sorted({relation.target_node_key for relation in relations}),
        },
    )


async def verify_projection(
    database: FalkorDBClient,
    nodes: Sequence[GraphNodeRecord],
    relations: Sequence[GraphEdgeRecord],
    *,
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
          AND node.graph_schema_version = $graph_schema_version
        RETURN node.node_key AS node_key, count(node) AS occurrences
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "node_keys": list(node_keys),
        },
    )
    relation_rows = await read_rows(
        database,
        """
        MATCH (source:KnowledgeNode)-[relation]->(target:KnowledgeNode)
        WHERE relation.tenant_id = $tenant_id
          AND relation.graph_schema_version = $graph_schema_version
          AND relation.relation_id IN $relation_ids
        RETURN relation.relation_id AS relation_id,
               source.node_key AS source_node_key,
               target.node_key AS target_node_key,
               count(relation) AS occurrences
        """,
        {
            "tenant_id": str(context.tenant_id),
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "relation_ids": list(relation_ids),
        },
    )
    return build_graph_verification(
        node_keys=node_keys,
        relation_ids=relation_ids,
        node_rows=node_rows,
        relation_rows=relation_rows,
    )


def _node_row(node: GraphNodeRecord, *, tenant_id: str) -> dict[str, Any]:
    return {
        "node_key": node.node_key,
        "logical_id": node.logical_id,
        "node_kind": node.node_kind.value,
        "entity_type": node.entity_type.value,
        "graph_schema_version": node.graph_schema_version,
        "ownership_scope": node.ownership_scope.value,
        "owner_id": str(node.owner_id),
        "title": node.title,
        "section_path": list(node.section_path),
        "source_scope_id": node.source_scope_id,
        "document_id": (str(node.document_id) if node.document_id is not None else None),
        "document_version_id": (
            str(node.document_version_id) if node.document_version_id is not None else None
        ),
        "attributes": node.attributes,
        # Top-level copy of attributes["placeholder"]: FalkorDB stores attributes as a
        # JSON string, so the placeholder guard in upsert_nodes needs it as a property.
        "placeholder": node.attributes.get("placeholder") is True,
        "tenant_id": tenant_id,
    }


def _relation_row(relation: GraphEdgeRecord, *, tenant_id: str) -> dict[str, Any]:
    return {
        "relation_id": relation.relation_id,
        "relation_type": relation.relation_type.value,
        "source_node_key": relation.source_node_key,
        "target_node_key": relation.target_node_key,
        "graph_schema_version": relation.graph_schema_version,
        "ownership_scope": relation.ownership_scope.value,
        "owner_id": str(relation.owner_id),
        "source_scope_id": relation.source_scope_id,
        "document_id": (str(relation.document_id) if relation.document_id is not None else None),
        "document_version_id": (
            str(relation.document_version_id) if relation.document_version_id is not None else None
        ),
        "attributes": relation.attributes,
        "source_relation_version": relation.source_relation_version,
        "source_explicit": relation.source_explicit,
        "tenant_id": tenant_id,
    }
