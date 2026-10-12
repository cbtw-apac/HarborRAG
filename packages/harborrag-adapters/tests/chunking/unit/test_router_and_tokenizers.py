from __future__ import annotations

import logging
import sys

import pytest

from harborrag_adapters.chunking import (
    CodeStructureRefiner,
    DocumentStructureRefiner,
    RoutingTextRefiner,
    SeparatorRefiner,
    TextStructureRefiner,
    TiktokenTokenCounter,
    build_text_refiner,
)
from harborrag_core.contracts.chunking import (
    SplitBoundaryKind,
    TextRefinementRequest,
    TextRefiner,
    TokenCounter,
)

from .refiner_contract import CharacterCounter, assert_valid_splits


def request(content: str, kind: SplitBoundaryKind, **options) -> TextRefinementRequest:
    return TextRefinementRequest(content=content, maximum_tokens=20, boundary_kind=kind, **options)


@pytest.mark.parametrize(
    ("kind", "content", "options", "expected"),
    [
        (SplitBoundaryKind.CODE_BLOCK, "x = 1", {}, CodeStructureRefiner),
        (SplitBoundaryKind.CODE_SYMBOL, "x = 1", {"separators": ("\n", "")}, CodeStructureRefiner),
        (SplitBoundaryKind.TABLE_ROW, "a\tb", {}, SeparatorRefiner),
        (SplitBoundaryKind.JSON_PATH, '{"a": 1}', {}, SeparatorRefiner),
        (SplitBoundaryKind.LINE, "- a", {}, SeparatorRefiner),
        (SplitBoundaryKind.PARAGRAPH, "Plain text.", {"separators": ("\n", "")}, SeparatorRefiner),
        (SplitBoundaryKind.PARAGRAPH, "# Title\nBody", {}, DocumentStructureRefiner),
        (SplitBoundaryKind.SECTION, "Plain prose only.", {}, TextStructureRefiner),
    ],
)
def test_router_dispatch(kind, content, options, expected) -> None:
    router = build_text_refiner(CharacterCounter())

    assert isinstance(router.select(request(content, kind, **options)), expected)


def test_router_is_a_core_text_refiner_with_a_fingerprint() -> None:
    router = build_text_refiner(CharacterCounter())

    assert isinstance(router, TextRefiner)
    assert isinstance(router, RoutingTextRefiner)
    fingerprint = router.fingerprint()
    for name in ("text-structure:1", "document-structure:1", "code-structure:1", "separators:1"):
        assert name in fingerprint


def test_separator_refiner_keeps_lines_whole_when_they_fit() -> None:
    content = "".join(f"row {index}\tvalue {index}\n" for index in range(8))
    line_request = request(content, SplitBoundaryKind.TABLE, separators=("\n", ""))

    splits = SeparatorRefiner(CharacterCounter()).split(line_request)

    assert_valid_splits(line_request, splits, CharacterCounter())
    assert all(split.content.endswith("\n") for split in splits)


def test_router_splits_every_kind_within_the_limit() -> None:
    router = build_text_refiner(CharacterCounter())
    samples = {
        SplitBoundaryKind.PARAGRAPH: "Sentence one is here. Sentence two is here. Three.",
        SplitBoundaryKind.CODE_BLOCK: "def f():\n    a = 1\n    b = 2\n    return a + b\n",
        SplitBoundaryKind.TABLE_ROW: "cell one\tcell two\tcell three\tcell four",
    }
    for kind, content in samples.items():
        unit = request(content, kind)
        assert_valid_splits(unit, router.split(unit), CharacterCounter())


def test_factory_warns_once_when_tree_sitter_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    caplog.set_level(logging.WARNING)

    router = build_text_refiner(CharacterCounter())

    assert "chunking-code" in caplog.text
    unit = request("def f():\n    return 1\n" * 4, SplitBoundaryKind.CODE_BLOCK, language="python")
    assert_valid_splits(unit, router.split(unit), CharacterCounter())


class TestTiktoken:
    @pytest.fixture(autouse=True)
    def _tiktoken(self) -> None:
        tiktoken = pytest.importorskip("tiktoken")
        try:
            tiktoken.get_encoding("cl100k_base")
        except Exception as exc:  # encoding file not cached and no network
            pytest.skip(f"cl100k_base encoding unavailable: {exc}")

    def test_counts_match_tiktoken_and_accept_special_tokens(self) -> None:
        import tiktoken

        counter = TiktokenTokenCounter()
        text = "Hello world <|endoftext|> and more"

        assert isinstance(counter, TokenCounter)
        assert counter.count(text) == len(
            tiktoken.get_encoding("cl100k_base").encode(text, allowed_special="all")
        )
        assert counter.count("") == 0
        assert counter.version == "cl100k_base"


def test_tiktoken_counter_reports_the_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)

    with pytest.raises(ImportError, match="chunking-tokens"):
        TiktokenTokenCounter()
