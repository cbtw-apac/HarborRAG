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
    KnowledgeNodeKind,
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
            WITH node, row, node.description AS described_description,
                 node.description_build_id AS description_build_id,
                 node.title AS described_title,
                 node.name AS described_name
            SET node = row
            SET node.description = CASE
                    WHEN description_build_id IS NOT NULL
                    THEN described_description ELSE row.description END,
                node.description_build_id = description_build_id,
                node.name = CASE
                    WHEN row.node_kind = 'Chunk' AND description_build_id IS NOT NULL
                    THEN described_name ELSE row.name END,
                node.title = CASE
                    WHEN row.node_kind = 'Chunk' AND description_build_id IS NOT NULL
                    THEN described_title ELSE row.title END
            SET node.title_key = toLower(node.title)
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
            SET node.title_key = toLower(node.title)
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

    Relations only: the far end a retraction leaves edgeless is deliberately not deleted
    here. Deleting it means testing its degree in one transaction and deleting it in
    another, and ``write_projection`` stages nodes before their relations -- so a
    placeholder a concurrent projection has just staged reads as edgeless, and removing it
    makes that projection's ``MATCH target`` find nothing, write no relation and fail its
    own verification. Scoping the test to the keys just retracted does not help: a
    placeholder shared by several linking documents is exactly the node another writer is
    staging.

    Nothing reads an edgeless node -- every graph query starts from a node and walks at
    least one relationship -- so the stub is unreachable rather than wrong, and the
    tenant-wide prune on version and source-item cleanup reaps it. A transient node count
    is the right thing to trade against a failed ingestion.
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
    shared = node.node_kind == KnowledgeNodeKind.SOURCE_ENTITY
    return {
        "name": _display_name(node, shared=shared),
        "description": node.description or _structural_description(node),
        "node_key": node.node_key,
        "logical_id": node.logical_id,
        "node_kind": node.node_kind.value,
        "entity_type": node.entity_type.value,
        "graph_schema_version": node.graph_schema_version,
        "ownership_scope": node.ownership_scope.value,
        # Provider metadata is an observation on version-owned support edges.
        # A staged or failed version must not overwrite shared, visible metadata.
        # title_key is deliberately absent: the store derives it from the title it
        # actually stored, which for a chunk with a generated title is not this one.
        "title": node.logical_id[:512] if shared else node.title,
        "section_path": list(node.section_path),
        "source_scope_id": node.source_scope_id,
        "document_id": (str(node.document_id) if node.document_id is not None else None),
        "document_version_id": (
            str(node.document_version_id) if node.document_version_id is not None else None
        ),
        "attributes": {} if shared else node.attributes,
        # Top-level copy of attributes["placeholder"]: FalkorDB stores attributes as a
        # JSON string, so the placeholder guard in upsert_nodes needs it as a property.
        **({"placeholder": True} if node.attributes.get("placeholder") is True else {}),
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
        "source_scope_id": relation.source_scope_id,
        "document_id": (str(relation.document_id) if relation.document_id is not None else None),
        "document_version_id": (
            str(relation.document_version_id) if relation.document_version_id is not None else None
        ),
        "attributes": relation.attributes,
        "source_relation_version": relation.source_relation_version,
        "source_explicit": relation.source_explicit,
        "source_title": _observation_title(relation, "source"),
        "target_title": _observation_title(relation, "target"),
        "source_relation": relation.attributes.get("source_relation") is True,
        "tenant_id": tenant_id,
    }


def _observation_title(relation: GraphEdgeRecord, role: str) -> str | None:
    observation = relation.attributes.get(f"{role}_metadata")
    return observation.get("title") if isinstance(observation, dict) else None


def _display_name(node: GraphNodeRecord, *, shared: bool) -> str:
    """Name the thing, not its identifier.

    ``name`` is a display field stripped from every read contract (see
    ``knowledge_mapping._INTERNAL_PROPERTY_KEYS``), so it is free to carry the most
    legible label available. ``title``/``title_key`` remain the version-safe lookup
    identity, which is why a shared node keeps its provider id there even when a
    human title is known here.
    """

    if node.title:
        # A leaf heading repeats across documents ("Overview", "Recipes"); its ancestry
        # is what makes it identifiable. section_path already carries it.
        if node.node_kind == KnowledgeNodeKind.STRUCTURE and len(node.section_path) > 1:
            return " › ".join(node.section_path)[:512]
        return node.title[:512]
    if shared:
        return node.logical_id[:512]
    if node.node_kind == KnowledgeNodeKind.CHUNK:
        ordinal = node.attributes.get("ordinal")
        return f"Chunk {int(ordinal) + 1}" if isinstance(ordinal, int) else "Chunk"
    return node.entity_type.value.replace("_", " ").title()


def _structural_description(node: GraphNodeRecord) -> str:
    kind = node.entity_type.value.replace("_", " ")
    if node.node_kind == KnowledgeNodeKind.CHUNK:
        location = " / ".join(node.section_path)
        return f"Evidence chunk in {location}." if location else "Document evidence chunk."
    if node.node_kind == KnowledgeNodeKind.STRUCTURE and node.title:
        return f"Document {kind}: {node.title}."
    return f"{kind.title()} in the source topology."
