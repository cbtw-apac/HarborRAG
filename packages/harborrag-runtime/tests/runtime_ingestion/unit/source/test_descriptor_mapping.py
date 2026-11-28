"""A local file's relative path is its source item ID, edge spaces included."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.descriptors import default_document_descriptor
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import DiscoveredSourceItem
from harborrag_runtime.ingestion.source.descriptor_mapping import SourceDescriptorMapper

from ...fixtures.release import source_request

pytestmark = pytest.mark.unit


def _discovered(relative_path: str) -> DiscoveredSourceItem:
    record = SourceRecord(
        id=relative_path,
        source_type="text/markdown",
        locator=relative_path,
        metadata={"relative_path": relative_path},
    )
    (pending,) = SourceDescriptorMapper().releases(
        source_request("task-1"), default_document_descriptor(record)
    )
    return pending.item


def test_local_paths_that_differ_only_in_edge_spaces_stay_separate_documents() -> None:
    items = [_discovered(path) for path in ("notes.md", " notes.md", "notes.md ")]

    assert [item.source_identity.source_item_id for item in items] == [
        "notes.md",
        " notes.md",
        "notes.md ",
    ]
    assert len({item.document_id for item in items}) == 3


def test_a_windows_separator_becomes_a_portable_one() -> None:
    assert _discovered("docs\\notes.md").source_identity.source_item_id == "docs/notes.md"


@pytest.mark.parametrize(
    "relative_path", ["  ", "/etc/hosts", "../notes.md", "docs/../../notes.md"]
)
def test_a_blank_or_escaping_relative_path_is_refused(relative_path: str) -> None:
    with pytest.raises(ValueError, match="relative_path"):
        _discovered(relative_path)
