"""Routing table, default factory, grammar-set guard and language helpers."""

from __future__ import annotations

import logging

import pytest

from harborrag_adapters.chunking import (
    CodeStructureRefiner,
    DocumentStructureRefiner,
    GrammarSetMismatch,
    SeparatorRefiner,
    TextStructureRefiner,
    build_text_refiner,
    code_structure,
    factory,
    guess_language,
    normalize_language,
    text_refiner_fingerprint,
)
from harborrag_adapters.chunking.languages import GRAMMAR_SET
from harborrag_core.contracts.chunking import SplitBoundaryKind, TextRefinementRequest


def _no_parser(language: str) -> None:
    return None


@pytest.mark.parametrize(
    ("boundary_kind", "content", "separators", "expected"),
    [
        (SplitBoundaryKind.CODE_BLOCK, "x = 1", ("\n", ""), CodeStructureRefiner),
        (SplitBoundaryKind.CODE_SYMBOL, "x = 1", None, CodeStructureRefiner),
        (SplitBoundaryKind.TABLE_ROW, "a\tb", ("\n", ""), SeparatorRefiner),
        (SplitBoundaryKind.JSON_PATH, "{}", None, SeparatorRefiner),
        (SplitBoundaryKind.LINE, "- a", None, SeparatorRefiner),
        (SplitBoundaryKind.PARAGRAPH, "plain", ("\n", ""), SeparatorRefiner),
        (SplitBoundaryKind.PARAGRAPH, "# Title\n\ntext", None, DocumentStructureRefiner),
        (SplitBoundaryKind.PARAGRAPH, "Plain prose. More.", None, TextStructureRefiner),
    ],
)
def test_router_picks_the_refiner_for_the_unit(
    words, boundary_kind, content, separators, expected
) -> None:
    refiner = build_text_refiner(words, parser_loader=_no_parser)
    request = TextRefinementRequest(
        content=content,
        maximum_tokens=5,
        boundary_kind=boundary_kind,
        separators=separators,
    )

    assert isinstance(refiner.select(request), expected)


def test_fingerprint_names_every_strategy_and_declares_the_grammar_set() -> None:
    fingerprint = text_refiner_fingerprint()

    assert fingerprint.startswith("routing:1(")
    for part in ("text-structure", "document-structure", "code-structure", "separators"):
        assert part in fingerprint
    assert GRAMMAR_SET in fingerprint


def test_fingerprint_does_not_depend_on_installed_grammars(monkeypatch) -> None:
    # The API computes the processing identity without the grammars installed,
    # so the fingerprint must come from the declared set, never the environment.
    installed = text_refiner_fingerprint()
    monkeypatch.setattr(factory, "grammar_set_problems", lambda: ["tree-sitter-go missing"])

    assert text_refiner_fingerprint() == installed


def test_missing_grammars_are_fatal_by_default(words, monkeypatch) -> None:
    monkeypatch.setattr(factory, "grammar_set_problems", lambda: ["tree-sitter-go missing"])

    with pytest.raises(GrammarSetMismatch, match="tree-sitter-go missing"):
        build_text_refiner(words)


def test_the_separator_fallback_must_be_requested_and_is_logged(words, monkeypatch, caplog) -> None:
    monkeypatch.setattr(factory, "grammar_set_problems", lambda: ["tree-sitter-go missing"])
    # No grammar loads, as on a worker without them installed.
    monkeypatch.setattr(code_structure, "default_parser_loader", _no_parser)

    with caplog.at_level(logging.WARNING):
        refiner = build_text_refiner(words, require_grammars=False)

    assert "tree-sitter-go missing" in caplog.text
    code = (
        "class Job:\n"
        "    def run(self):\n        a = 1\n        b = 2\n        return a + b\n\n"
        "    def stop(self):\n        c = 3\n        d = 4\n        return c + d\n"
    )
    splits = refiner.split(
        TextRefinementRequest(
            content=code,
            maximum_tokens=12,
            boundary_kind=SplitBoundaryKind.CODE_BLOCK,
            language="python",
        )
    )
    # Separator tier: cut at the method definitions, with no syntax-tier scope.
    assert "".join(split.content for split in splits) == code
    assert [split.content.lstrip().split("\n", 1)[0] for split in splits[1:]] == [
        "def run(self):",
        "def stop(self):",
    ]
    assert all(split.prefix is None for split in splits)


def test_an_injected_parser_loader_skips_the_grammar_check(words, monkeypatch) -> None:
    monkeypatch.setattr(factory, "grammar_set_problems", lambda: ["tree-sitter missing"])

    build_text_refiner(words, parser_loader=_no_parser)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"fence_info": "py"}, "python"),
        ({"fence_info": "TypeScript title=x.ts"}, "typescript"),
        ({"fence_info": "{.rust}"}, "rust"),
        ({"fence_info": "  ", "filename": "src/app.tsx"}, "tsx"),
        ({"filename": "C:\\code\\main.go"}, "go"),
        ({"filename": "notes.txt"}, None),
        ({}, None),
    ],
)
def test_guess_language(kwargs, expected) -> None:
    assert guess_language(**kwargs) == expected


def test_normalize_language() -> None:
    assert normalize_language(" C++ ") == "cpp"
    assert normalize_language("") is None
    assert normalize_language(None) is None
