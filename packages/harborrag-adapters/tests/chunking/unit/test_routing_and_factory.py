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
    factory,
    guess_language,
    normalize_language,
    text_refiner_fingerprint,
)
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


def test_fingerprint_names_every_strategy_and_ignores_installed_extras() -> None:
    fingerprint = text_refiner_fingerprint()

    assert fingerprint.startswith("routing:1(")
    for part in ("text-structure", "document-structure", "code-structure", "separators"):
        assert part in fingerprint
    assert fingerprint == text_refiner_fingerprint()


def test_missing_grammars_fail_unless_the_fallback_is_accepted(words, monkeypatch, caplog) -> None:
    monkeypatch.setattr(factory, "grammar_set_problems", lambda: ["tree-sitter-go missing"])

    with pytest.raises(GrammarSetMismatch, match="tree-sitter-go missing"):
        build_text_refiner(words)

    with caplog.at_level(logging.WARNING):
        refiner = build_text_refiner(words, require_grammars=False)
    assert "tree-sitter-go missing" in caplog.text
    assert refiner.fingerprint() == text_refiner_fingerprint()


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
