from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from harborrag_adapters.connectors.local.relations import (
    _MARKDOWN_LINK,
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


# Unclosed link openers made the old pattern rescan the rest of the document from
# every "[": "[](" * 20_000 (60 KB) took about 19 s and 120 KB about a minute,
# holding the GIL inside connector.load.
ADVERSARIAL_LINK_INPUTS = {
    "unclosed link openers": "[](" * 20_000,
    "unclosed labels": "[" * 60_000,
    "unclosed titles": '[](a "' * 10_000,
    "mixed title quotes": "[](a \"[](a '" * 5_000,
    "image openers": "![a](" * 12_000,
}


@pytest.mark.parametrize(
    "content", ADVERSARIAL_LINK_INPUTS.values(), ids=ADVERSARIAL_LINK_INPUTS.keys()
)
def test_link_scan_is_linear_on_hostile_markup(tmp_path: Path, content: str) -> None:
    source = tmp_path / "hostile.md"

    started = time.perf_counter()
    relations = LocalDocumentRelationResolver(tmp_path).relations(
        source_path=source,
        content=content.encode("utf-8"),
        media_type="text/markdown",
    )
    elapsed = time.perf_counter() - started

    # The quadratic pattern needs 19 s or more here, so a generous bound still
    # separates the two on a loaded machine.
    assert relations == []
    assert elapsed < 5.0, f"link scan took {elapsed:.2f}s"


def test_common_markdown_link_forms_still_resolve(tmp_path: Path) -> None:
    for name in ("a.md", "b.md", "c.md", "d.md", "e.png", "f.md", "g.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    source = tmp_path / "start.md"
    source.write_text(
        "[plain](a.md) [titled](b.md \"Title\") [single](c.md 'Title')\n"
        "[angle](<d.md>) [![badge](e.png)](https://example.com) [frag](f.md#part)\n"
        '[quote](g.md "it\'s")\n',
        encoding="utf-8",
    )

    relations = LocalDocumentRelationResolver(tmp_path).relations(
        source_path=source,
        content=source.read_bytes(),
        media_type="text/markdown",
    )

    assert [relation["target_id"] for relation in relations] == [
        "a.md",
        "b.md",
        "c.md",
        "d.md",
        "e.png",
        "f.md",
        "g.md",
    ]


# The pattern as it was before the linear rewrite, kept to prove the rewrite
# extracts the same targets from ordinary documents.
_PREVIOUS_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")

ORDINARY_LINK_DOCUMENTS = [
    "[plain](a.md)",
    "[titled](b.md \"Title\") [single](c.md 'Title')",
    "[angle](<d.md>)",
    "[![badge](e.png)](https://example.com)",
    "[a [nested] label](f.md#part)",
    '![image](img/pic.png "Caption") text [next](g.md)',
    "[empty]() [spaced](  h.md) [bare] (i.md) [open](j.md",
    "# Title\n\nSee [one](one.md),\n[two](two.md 'T') and [three](<three.md>).\n",
]


@pytest.mark.parametrize("document", ORDINARY_LINK_DOCUMENTS)
def test_rewritten_link_pattern_matches_previous_targets(document: str) -> None:
    assert _MARKDOWN_LINK.findall(document) == _PREVIOUS_MARKDOWN_LINK.findall(document)
