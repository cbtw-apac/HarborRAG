from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.connectors.local.relations import (
    LocalDocumentRelationResolver,
)

pytestmark = pytest.mark.unit


def test_local_markdown_links_resolve_to_source_document_ids(
    tmp_path: Path,
) -> None:
    """A relative Markdown link resolves to the target's path under the root."""
    source = tmp_path / "guides" / "start.md"
    target = tmp_path / "reference.md"
    source.parent.mkdir()
    source.write_text("[Reference](../reference.md)", encoding="utf-8")
    target.write_text("reference", encoding="utf-8")

    relations = LocalDocumentRelationResolver(tmp_path).relations(
        source_path=source,
        content=source.read_bytes(),
        media_type="text/markdown",
    )

    assert relations == [
        {
            "predicate": "links_to",
            "target_id": "reference.md",
            "target_type": "document",
            "metadata": {"source_link": "../reference.md"},
        }
    ]


@pytest.mark.parametrize(
    "link",
    [
        "<sub dir/notes file.md>",
        '<sub dir/notes file.md> "Team notes"',
        "sub%20dir/notes%20file.md",
    ],
)
def test_local_markdown_link_to_a_path_with_spaces_resolves(tmp_path: Path, link: str) -> None:
    """Angle-bracket and percent-encoded links both reach a path that holds spaces."""
    source = tmp_path / "start.md"
    target = tmp_path / "sub dir" / "notes file.md"
    target.parent.mkdir()
    source.write_text(f"[Notes]({link})", encoding="utf-8")
    target.write_text("notes", encoding="utf-8")

    relations = LocalDocumentRelationResolver(tmp_path).relations(
        source_path=source,
        content=source.read_bytes(),
        media_type="text/markdown",
    )

    assert [relation["target_id"] for relation in relations] == ["sub dir/notes file.md"]


@pytest.mark.parametrize("link", ["< leading.md>", "%20leading.md"])
def test_local_markdown_link_to_a_name_with_a_leading_space_resolves(
    tmp_path: Path, link: str
) -> None:
    """A link keeps the leading space of its target name instead of dropping it."""
    source = tmp_path / "start.md"
    (tmp_path / " leading.md").write_text("target", encoding="utf-8")
    source.write_text(f"[Target]({link})", encoding="utf-8")

    relations = LocalDocumentRelationResolver(tmp_path).relations(
        source_path=source,
        content=source.read_bytes(),
        media_type="text/markdown",
    )

    assert [relation["target_id"] for relation in relations] == [" leading.md"]


def test_local_markdown_named_with_a_trailing_space_is_read_for_links(tmp_path: Path) -> None:
    """A Markdown file whose name ends in a space is still read for links."""
    source = tmp_path / "start.md "
    (tmp_path / "reference.md").write_text("reference", encoding="utf-8")
    source.write_text("[Reference](reference.md)", encoding="utf-8")

    relations = LocalDocumentRelationResolver(tmp_path).relations(
        source_path=source,
        content=source.read_bytes(),
        media_type="application/octet-stream",
    )

    assert [relation["target_id"] for relation in relations] == ["reference.md"]


def test_local_link_outside_source_root_is_ignored(
    tmp_path: Path,
) -> None:
    """A link that leaves the source root produces no relation."""
    root = tmp_path / "root"
    root.mkdir()
    source = root / "start.md"
    source.write_text("[Escape](../../outside.md)", encoding="utf-8")

    relations = LocalDocumentRelationResolver(root).relations(
        source_path=source,
        content=source.read_bytes(),
        media_type="text/markdown",
    )

    assert relations == []
