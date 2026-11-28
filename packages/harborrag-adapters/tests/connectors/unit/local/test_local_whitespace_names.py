"""Regression: local file names that end in whitespace keep their extension."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.connectors.local.config import LocalFileConfig
from harborrag_adapters.connectors.local.connector import LocalFileConnector

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "name", ["notes.md ", "notes.md\u00a0", "notes.md\u200b", "Q3 design review.md"]
)
def test_local_file_name_with_whitespace_keeps_its_markdown_extension(
    tmp_path: Path, name: str
) -> None:
    """A space after the extension neither hides the file nor loses its media type."""
    (tmp_path / name).write_text("# Notes\n\nHello.\n", encoding="utf-8")
    connector = LocalFileConnector(LocalFileConfig(source_path=tmp_path))

    records = list(connector.discover())
    raw = connector.load(records[0])

    assert [record.id for record in records] == [name]
    assert raw.content_type == "text/markdown"
    assert raw.metadata["suffix"] == ".md"


def test_local_folder_named_with_a_trailing_space_can_be_excluded(tmp_path: Path) -> None:
    """An excluded folder name keeps its trailing space and is skipped."""
    (tmp_path / "build ").mkdir()
    (tmp_path / "build " / "generated.md").write_text("generated", encoding="utf-8")
    (tmp_path / "keep.md").write_text("keep", encoding="utf-8")
    connector = LocalFileConnector(
        LocalFileConfig(source_path=tmp_path, excluded_dir_names={"build "})
    )

    assert [record.id for record in connector.discover()] == ["keep.md"]
