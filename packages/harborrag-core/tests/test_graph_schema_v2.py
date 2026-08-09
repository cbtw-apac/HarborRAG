from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)


def test_graph_schema_v2_accepts_extensible_entity_types_and_stable_ownership() -> None:
    node = GraphNodeRecord(
        node_key="stable-key",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type="custom_catalog_record",
        logical_id="provider-1",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        title="Catalog record",
        attributes={"provider_id": "provider-1", "ordinal": 2},
    )

    assert node.graph_schema_version == "2.0"
    assert node.entity_type == GraphEntityType("custom_catalog_record")
    assert node.document_id is None
    assert node.document_version_id is None


@pytest.mark.parametrize("field", ["content", "body", "preview", "raw_payload", "password"])
def test_graph_schema_v2_rejects_content_and_credentials(field: str) -> None:
    with pytest.raises(ValidationError, match="graph attribute field is not allowed"):
        GraphNodeRecord(
            node_key="stable-key",
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
            logical_id="provider-1",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
            attributes={field: "must-not-persist"},
        )


def test_graph_schema_v2_rejects_non_allowlisted_provider_attributes() -> None:
    with pytest.raises(ValidationError, match="not allowlisted"):
        GraphNodeRecord(
            node_key="stable-key",
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
            logical_id="provider-1",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
            attributes={"arbitrary_provider_blob": "not allowed"},
        )


def test_graph_schema_v2_requires_ids_only_for_version_owned_records() -> None:
    with pytest.raises(ValidationError, match="requires document and version IDs"):
        GraphNodeRecord(
            node_key="version-key",
            node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
            entity_type=GraphEntityType.DOCUMENT_VERSION,
            logical_id="version-1",
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            owner_id="tenant-1",
            source_scope_id="scope-1",
        )

    with pytest.raises(ValidationError, match="only version-owned"):
        GraphNodeRecord(
            node_key="source-key",
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
            logical_id="item-1",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
            document_id="document-1",
            document_version_id="version-1",
        )


def test_chunk_identity_and_relationship_ownership_are_enforced() -> None:
    with pytest.raises(ValidationError, match="exact chunk ID"):
        GraphNodeRecord(
            node_key="not-the-chunk-id",
            node_kind=KnowledgeNodeKind.CHUNK,
            entity_type=GraphEntityType.CHUNK,
            logical_id="chunk-1",
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            owner_id="tenant-1",
            source_scope_id="scope-1",
            document_id="document-1",
            document_version_id="version-1",
        )

    with pytest.raises(ValidationError, match="has_version"):
        GraphEdgeRecord(
            relation_id="relation-1",
            relation_type=RelationType.HAS_VERSION,
            source_node_key="source-1",
            target_node_key="version-1",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
            source_relation_version="2.0",
            source_explicit=False,
        )
