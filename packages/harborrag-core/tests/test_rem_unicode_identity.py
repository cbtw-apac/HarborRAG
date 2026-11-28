"""Regression: source item IDs that differ only in Unicode form get different identities."""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import SourceIdentity
from harborrag_core.ingestion.identity import (
    DocumentIdentityBuilder,
    composed_owner_of_legacy_identity,
    identity_for_source,
    legacy_identity_for_source,
)

pytestmark = pytest.mark.unit

IDENTITIES = DocumentIdentityBuilder()
COMPOSED = "docs/caf\u00e9.md"
DECOMPOSED = "docs/cafe\u0301.md"


def _source(
    source_item_id: str, connector_type: ConnectorType = ConnectorType.LOCAL
) -> SourceIdentity:
    return SourceIdentity(
        tenant_id="acme",
        connector_type=connector_type,
        connection_id="local-docs",
        source_item_id=source_item_id,
        source_scope_id="scope-docs",
    )


@pytest.mark.parametrize(
    ("source_item_id", "pinned_document_id"),
    [
        (
            "docs/release-notes.md",
            "document:bca10bde39eb1bfe8c463b73a4198d0d94c69931d80696cf124b72dcd0f49b1a",
        ),
        (
            COMPOSED,
            "document:765068ba42d34831ceb0801242cb7cdc9a0062517b2597bb15a52f60a2dff98f",
        ),
        (
            "docs/Q3\u00a0cafe\u0301.md",
            "document:b32308d706356d652e6eda90efce73e08f6412d9d4fbc0c74e96362307fd4aa8",
        ),
    ],
)
def test_document_id_keeps_the_value_4fe8b68e_gave_it(
    source_item_id: str, pinned_document_id: str
) -> None:
    """The pinned IDs were computed with the identity code of commit 4fe8b68e."""
    assert identity_for_source(_source(source_item_id)) == pinned_document_id


@pytest.mark.parametrize("connector_type", list(ConnectorType))
@pytest.mark.parametrize("source_item_id", ["DEMO-42", "jira://DEMO/DEMO-42", COMPOSED])
def test_an_nfc_source_item_id_keeps_its_legacy_document_id(
    connector_type: ConnectorType, source_item_id: str
) -> None:
    source = _source(source_item_id, connector_type)

    assert identity_for_source(source) == legacy_identity_for_source(source)


def test_composed_and_decomposed_spellings_get_two_document_ids() -> None:
    composed = identity_for_source(_source(COMPOSED))
    decomposed = identity_for_source(_source(DECOMPOSED))

    assert composed != decomposed
    assert legacy_identity_for_source(_source(DECOMPOSED)) == composed


def test_composed_and_decomposed_spellings_get_two_graph_keys() -> None:
    source_keys = {
        IDENTITIES.source_entity_node_key(
            tenant_id="acme",
            source_scope_id="scope-docs",
            entity_type="local_file",
            provider_id=path,
        )
        for path in (COMPOSED, DECOMPOSED)
    }
    external_keys = {
        IDENTITIES.external_source_entity_node_key(
            tenant_id="acme",
            connector_type="local",
            connection_id="local-docs",
            source_item_id=path,
        )
        for path in (COMPOSED, DECOMPOSED)
    }

    assert len(source_keys) == 2
    assert len(external_keys) == 2
    assert IDENTITIES.source_entity_node_key(
        tenant_id="acme",
        source_scope_id="scope-docs",
        entity_type="local_file",
        provider_id=COMPOSED,
    ) == ("graph-v2-source-entity:1beada65e3344ca2cc4eb96ab22cbca04ba3ed8a4c92e752807db4d7ba12b58f")


def test_the_nfc_spelling_owns_the_legacy_id_of_a_decomposed_spelling() -> None:
    owner = composed_owner_of_legacy_identity(_source(DECOMPOSED))

    assert owner == _source(COMPOSED)
    assert identity_for_source(owner) == legacy_identity_for_source(_source(DECOMPOSED))


@pytest.mark.parametrize("source_item_id", [COMPOSED, "docs/Q3\u00a0cafe\u0301.md"])
def test_no_other_spelling_owns_the_legacy_id_of_an_nfc_or_lossy_spaced_id(
    source_item_id: str,
) -> None:
    assert composed_owner_of_legacy_identity(_source(source_item_id)) is None
