"""node_kind and entity_type are one taxonomy with a defined relationship, not two."""

from __future__ import annotations

import pytest

from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)

pytestmark = pytest.mark.unit


def _node(**overrides: object) -> GraphNodeRecord:
    base: dict[str, object] = {
        "node_key": "node-1",
        "node_kind": KnowledgeNodeKind.STRUCTURE,
        "entity_type": GraphEntityType.SECTION,
        "logical_id": "section-1",
        "ownership_scope": GraphOwnershipScope.DOCUMENT_VERSION,
        "owner_id": "tenant-a",
        "source_scope_id": "scope-a",
        "document_id": "document-1",
        "document_version_id": "version-1",
    }
    return GraphNodeRecord(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "entity_type",
    [GraphEntityType.SECTION, GraphEntityType.TABLE, GraphEntityType.COMMENT],
)
def test_structure_accepts_its_closed_structural_vocabulary(entity_type) -> None:
    assert _node(entity_type=entity_type).entity_type is entity_type


def test_structure_rejects_a_provider_entity_type() -> None:
    # Nothing prevented this today, yet _display_name branches on node_kind while
    # _structural_description branches on entity_type -- a mismatch renders nonsense.
    with pytest.raises(ValueError, match="structure"):
        _node(entity_type=GraphEntityType.CONFLUENCE_PAGE)


def test_source_entity_rejects_a_reserved_structural_entity_type() -> None:
    with pytest.raises(ValueError, match="source_entity"):
        _node(
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.CHUNK,
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            document_id=None,
            document_version_id=None,
        )


def test_source_entity_stays_open_to_new_provider_types() -> None:
    # GraphEntityType._missing_ admits custom values on purpose; closing the set here
    # would break every connector added after this file.
    node = _node(
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType("notion_page"),
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        document_id=None,
        document_version_id=None,
    )

    assert node.entity_type.value == "notion_page"


@pytest.mark.parametrize(
    ("node_kind", "expected"),
    [
        (KnowledgeNodeKind.TENANT, GraphEntityType.TENANT),
        (KnowledgeNodeKind.DATA_SOURCE, GraphEntityType.DATA_SOURCE),
        (KnowledgeNodeKind.DOCUMENT_VERSION, GraphEntityType.DOCUMENT_VERSION),
        (KnowledgeNodeKind.CHUNK, GraphEntityType.CHUNK),
    ],
)
def test_the_four_forced_entity_types_need_not_be_restated(node_kind, expected) -> None:
    # These four are already validated to have exactly one legal value, so requiring
    # callers to repeat them is duplication the model can remove.
    scope = {
        KnowledgeNodeKind.TENANT: GraphOwnershipScope.TENANT,
        KnowledgeNodeKind.DATA_SOURCE: GraphOwnershipScope.SOURCE_SCOPE,
        KnowledgeNodeKind.DOCUMENT_VERSION: GraphOwnershipScope.DOCUMENT_VERSION,
        KnowledgeNodeKind.CHUNK: GraphOwnershipScope.DOCUMENT_VERSION,
    }[node_kind]
    fields: dict[str, object] = {
        "node_kind": node_kind,
        "ownership_scope": scope,
        "logical_id": "logical-1",
    }
    if node_kind is KnowledgeNodeKind.CHUNK:
        fields["node_key"] = "logical-1"
    if scope is GraphOwnershipScope.TENANT:
        fields["source_scope_id"] = None
        fields["document_id"] = None
        fields["document_version_id"] = None
    elif scope is GraphOwnershipScope.SOURCE_SCOPE:
        fields["document_id"] = None
        fields["document_version_id"] = None

    node = GraphNodeRecord.model_validate(
        {
            "node_key": fields.get("node_key", "node-1"),
            "owner_id": "tenant-a",
            "source_scope_id": "scope-a",
            "document_id": "document-1",
            "document_version_id": "version-1",
            **fields,
        }
    )

    assert node.entity_type is expected
