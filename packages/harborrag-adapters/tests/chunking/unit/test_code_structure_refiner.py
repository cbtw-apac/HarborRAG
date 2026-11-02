"""Code refinement: tree-sitter syntax tier, separator tier, enclosing scope."""

from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.chunking import CodeStructureRefiner
from harborrag_core.contracts.chunking import SourceSpan, SplitBoundaryKind, TextRefinementRequest

CODE = '''class Foo:
    """Doc."""

    def a(self):
        x = 1
        y = 2
        return x + y

    # helper comment
    def b(self):
        return 3


def top():
    return 4
'''


def _request(language: str | None) -> TextRefinementRequest:
    return TextRefinementRequest(
        content=CODE,
        maximum_tokens=12,
        boundary_kind=SplitBoundaryKind.CODE_BLOCK,
        source_span=SourceSpan(
            start_offset=100,
            end_offset=100 + len(CODE),
            start_line=10,
            end_line=24,
        ),
        language=language,
    )


def _no_parser(language: str) -> Any | None:
    return None


def test_syntax_tier_keeps_methods_whole_with_their_comment_and_class_scope(
    words, assert_tiles
) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    request = _request("py")

    splits = CodeStructureRefiner(words).split(request)

    assert [split.content for split in splits] == [
        'class Foo:\n    """Doc."""\n\n',
        "    def a(self):\n        x = 1\n        y = 2\n        return x + y\n\n",
        "    # helper comment\n    def b(self):\n        return 3\n\n\n",
        "def top():\n    return 4\n",
    ]
    assert [split.prefix for split in splits] == [None, "class Foo:", "class Foo:", None]
    assert [split.source_span.start_line for split in splits] == [10, 13, 18, 23]
    assert {split.boundary_kind for split in splits} == {SplitBoundaryKind.CODE_SYMBOL}
    assert not any(split.forced_split for split in splits)
    assert_tiles(request, splits)


def test_separator_tier_cuts_at_definitions_without_a_parser(words, assert_tiles) -> None:
    request = _request("python")

    splits = CodeStructureRefiner(words, parser_loader=_no_parser).split(request)

    starts = [split.content.lstrip().split("\n", 1)[0] for split in splits]
    assert "def a(self):" in starts
    assert "def top():" in starts
    assert all(split.prefix is None for split in splits)
    assert_tiles(request, splits)


def test_unknown_language_and_missing_language_use_the_separator_tier(words) -> None:
    loaded: list[str] = []

    def loader(language: str) -> Any | None:
        loaded.append(language)
        return None

    refiner = CodeStructureRefiner(words, parser_loader=loader)
    without = refiner.split(_request(None))
    unknown = refiner.split(_request("cobol"))

    assert [split.content for split in without] == [split.content for split in unknown]
    assert loaded == ["cobol"]


def test_fingerprint_declares_the_grammar_set() -> None:
    fingerprint = CodeStructureRefiner(object(), parser_loader=_no_parser).fingerprint()

    assert fingerprint.startswith("code-structure:")
    assert "python-0.25" in fingerprint
