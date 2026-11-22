"""Unit tests for local source-record identity."""

from __future__ import annotations

from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.local.filesystem_paths import (
    LOCAL_RECORD_ID_PREFIX,
    local_record_id,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_local_record_id_is_stable_and_whitespace_free() -> None:
    first = local_record_id("Offers/AI Assisted Development 1-pager - Sales version.docx")
    second = local_record_id("Offers/AI Assisted Development 1-pager - Sales version.docx")

    assert first == second
    assert first.startswith(LOCAL_RECORD_ID_PREFIX)
    assert len(first) == len(LOCAL_RECORD_ID_PREFIX) + 64
    assert not any(character.isspace() for character in first)
    assert local_record_id("a.md") != local_record_id("b.md")


def test_file_names_with_whitespace_discover_and_load(tmp_path: Path) -> None:
    path = write_file(tmp_path / "Meeting notes" / "Sales AI Committee 17 Sep.md", b"# Notes")
    connector = LocalFileConnector(config(tmp_path))

    record = next(connector.discover(ConnectorQuery(filters={"file_paths": [path]})))
    document = connector.load(record)

    relative = "Meeting notes/Sales AI Committee 17 Sep.md"
    assert record.id == local_record_id(relative)
    assert record.locator == relative
    assert record.metadata["relative_path"] == relative
    assert document.id == record.id
    assert document.metadata["record_id"] == record.id
    assert document.metadata["relative_path"] == relative
    assert document.source == f"local:///{relative}"


def test_markdown_links_between_files_with_spaces_resolve_to_hashed_ids(tmp_path: Path) -> None:
    target = write_file(tmp_path / "Reference docs" / "Kill criteria.md", b"criteria")
    source = write_file(
        tmp_path / "Plans" / "Q4 plan.md",
        b"See [criteria](../Reference%20docs/Kill%20criteria.md)",
    )
    connector = LocalFileConnector(config(tmp_path))
    record = next(connector.discover(ConnectorQuery(filters={"file_paths": [source]})))

    document = connector.load(record)

    assert document.metadata["relations"] == [
        {
            "predicate": "links_to",
            "target_id": local_record_id("Reference docs/Kill criteria.md"),
            "target_type": "document",
            "metadata": {"source_link": "../Reference%20docs/Kill%20criteria.md"},
        }
    ]
    assert target.exists()
