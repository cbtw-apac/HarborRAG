"""A node's display name must identify the thing, not its opaque identifier."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb.knowledge_writes import _node_row
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
        "node_kind": KnowledgeNodeKind.SOURCE_ENTITY,
        "entity_type": GraphEntityType.CONFLUENCE_PAGE,
        "logical_id": "91980256",
        "ownership_scope": GraphOwnershipScope.SOURCE_SCOPE,
        "owner_id": "tenant-a",
        "source_scope_id": "scope-a",
    }
    return GraphNodeRecord(**{**base, **overrides})  # type: ignore[arg-type]


def test_confluence_page_displays_its_human_title_not_its_page_id() -> None:
    row = _node_row(_node(title="Handover Document"), tenant_id="tenant-a")

    assert row["name"] == "Handover Document"


def test_shared_node_identity_still_resists_a_staged_version_overwrite() -> None:
    # title/title_key remain the version-safe identifier: only `name` is a display field.
    row = _node_row(_node(title="Handover Document"), tenant_id="tenant-a")

    assert row["title"] == "91980256"


def test_shared_node_without_a_title_still_falls_back_to_its_identifier() -> None:
    row = _node_row(_node(), tenant_id="tenant-a")

    assert row["name"] == "91980256"


def test_section_displays_its_ancestry_so_common_leaf_names_are_distinguishable() -> None:
    row = _node_row(
        _node(
            node_kind=KnowledgeNodeKind.STRUCTURE,
            entity_type=GraphEntityType.SECTION,
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            document_id="document-1",
            document_version_id="version-1",
            logical_id="section-1",
            title="Recipes",
            section_path=("Ingestion and Parsing", "Recipes"),
        ),
        tenant_id="tenant-a",
    )

    assert row["name"] == "Ingestion and Parsing › Recipes"
    # The lookup identity stays the section's own name, so EXACT_TITLE is unchanged.
    assert row["title"] == "Recipes"


def test_top_level_section_is_not_decorated() -> None:
    row = _node_row(
        _node(
            node_kind=KnowledgeNodeKind.STRUCTURE,
            entity_type=GraphEntityType.SECTION,
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            document_id="document-1",
            document_version_id="version-1",
            logical_id="section-1",
            title="Overview",
            section_path=("Overview",),
        ),
        tenant_id="tenant-a",
    )

    assert row["name"] == "Overview"
