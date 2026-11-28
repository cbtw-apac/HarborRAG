"""A file's access time never makes a stored discovery descriptor differ from a new one."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.database.ingestion_control.source_item_mapping import (
    registration_from_existing,
)
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    DiscoveredSourceItem,
    SourceAdmissionDecision,
    SourceIdentity,
)
from harborrag_core.schemas.ids import DocumentId

pytestmark = pytest.mark.unit

_CHANGE_KEY = "change-key-1"


def _descriptor(**metadata: object) -> dict[str, object]:
    return {
        "id": "notes.md",
        "source_type": "text/markdown",
        "locator": "notes.md",
        "checksum": "stat:44:1764000000000000000",
        "metadata": {"relative_path": "notes.md", "size": 44, **metadata},
    }


def _item(descriptor: dict[str, object]) -> DiscoveredSourceItem:
    return DiscoveredSourceItem(
        source_identity=SourceIdentity(
            tenant_id="default",
            connector_type=ConnectorType.LOCAL,
            connection_id="local-docs",
            source_item_id="notes.md",
            source_scope_id="docs",
        ),
        document_id=DocumentId("document:notes"),
        source_version="stat:44:1764000000000000000",
        admission_change_key=_CHANGE_KEY,
        descriptor=descriptor,
    )


def _decision(stored: dict[str, object], discovered: dict[str, object]) -> SourceAdmissionDecision:
    row = {
        "descriptor": stored,
        "admission_change_key": _CHANGE_KEY,
        "source_version": "stat:44:1764000000000000000",
    }
    return registration_from_existing(row, _item(discovered)).decision


@pytest.mark.parametrize(
    ("stored", "discovered"),
    [
        (_descriptor(accessed_at="2026-11-01T00:00:00+00:00"), _descriptor()),
        (
            _descriptor(accessed_at="2026-11-01T00:00:00+00:00"),
            _descriptor(accessed_at="2026-11-02T00:00:00+00:00"),
        ),
    ],
    ids=["stored-before-upgrade", "read-since"],
)
def test_an_access_time_alone_leaves_the_item_unchanged(
    stored: dict[str, object], discovered: dict[str, object]
) -> None:
    assert _decision(stored, discovered) is SourceAdmissionDecision.UNCHANGED


def test_other_metadata_still_makes_the_item_metadata_changed() -> None:
    stored = _descriptor(accessed_at="2026-11-01T00:00:00+00:00")

    assert _decision(stored, _descriptor(size=45)) is SourceAdmissionDecision.METADATA_CHANGED
