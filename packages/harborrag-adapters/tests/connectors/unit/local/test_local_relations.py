from __future__ import annotations

from pathlib import Path

from harborrag_adapters.connectors.local.relations import (
    LocalDocumentRelationResolver,
)


def test_local_markdown_links_resolve_to_source_document_ids(
    tmp_path: Path,
) -> None:
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


def test_local_link_outside_source_root_is_ignored(
    tmp_path: Path,
) -> None:
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
