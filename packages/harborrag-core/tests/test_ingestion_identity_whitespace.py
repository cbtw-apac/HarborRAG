"""Regression: source item IDs that differ only in spacing get different identities."""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ConnectorType
from harborrag_core.chunking.identity import encoded_identifier
from harborrag_core.ingestion.identity import DocumentIdentityBuilder

pytestmark = pytest.mark.unit

IDENTITIES = DocumentIdentityBuilder()
SPACING_VARIANTS = [
    "docs/notes.md",
    "docs/notes.md ",
    " docs/notes.md",
    "docs/Q3 notes.md",
    "docs/Q3  notes.md",
    "docs/Q3\u00a0notes.md",
    "docs/Q3\tnotes.md",
]


def _document_id(source_item_id: str) -> str:
    return IDENTITIES.document_id(
        tenant_id="acme",
        connector_type=ConnectorType.LOCAL,
        connection_id="docs",
        source_item_id=source_item_id,
    )


def test_document_ids_differ_when_source_item_ids_differ_only_in_spacing() -> None:
    """Every spacing variant of one path names its own document."""
    assert len({_document_id(value) for value in SPACING_VARIANTS}) == len(SPACING_VARIANTS)


@pytest.mark.parametrize(
    "source_item_id",
    ["docs/notes.md", "docs/Q3 notes.md", "jira://CPM/CPM-1", "sharepoint://site/drive/item"],
)
def test_document_id_without_lossy_spacing_keeps_its_stored_value(source_item_id: str) -> None:
    """An ID whose spacing text normalization leaves alone keeps the identity it always had."""
    expected = encoded_identifier(
        "document",
        {
            "connector_type": "local",
            "connection_id": "docs",
            "source_item_id": source_item_id,
            "tenant_id": "acme",
        },
    )

    assert _document_id(source_item_id) == expected


def test_source_entity_node_keys_differ_when_provider_ids_differ_only_in_spacing() -> None:
    """Graph nodes of files whose paths differ only in spacing stay apart."""
    keys = {
        IDENTITIES.source_entity_node_key(
            tenant_id="acme", source_scope_id="docs", entity_type="local_file", provider_id=value
        )
        for value in SPACING_VARIANTS
    }

    assert len(keys) == len(SPACING_VARIANTS)


def test_external_source_entity_node_keys_differ_when_ids_differ_only_in_spacing() -> None:
    """Stand-ins for link targets whose IDs differ only in spacing stay apart."""
    keys = {
        IDENTITIES.external_source_entity_node_key(
            tenant_id="acme", connector_type="local", connection_id="docs", source_item_id=value
        )
        for value in SPACING_VARIANTS
    }

    assert len(keys) == len(SPACING_VARIANTS)
