"""Invariants every HarborRAG text refiner must satisfy, shared by refiner tests."""

from __future__ import annotations

from harborrag_core.contracts.chunking import TextRefinementRequest, TextSplit, TokenCounter


class CharacterCounter:
    """Count one token per character so limits are easy to reason about."""

    name = "characters"
    version = "1"

    def count(self, text: str) -> int:
        return len(text)


class WordCounter:
    """Count whitespace-separated words, plus one per CJK character."""

    name = "words"
    version = "1"

    def count(self, text: str) -> int:
        cjk = sum(1 for character in text if "　" <= character <= "鿿")
        return len(text.split()) + cjk


def assert_valid_splits(
    request: TextRefinementRequest,
    splits: tuple[TextSplit, ...],
    counter: TokenCounter,
) -> None:
    """Splits fit the limit, are ordered, non-empty, and tile the request exactly."""

    assert splits, "a non-blank request must produce splits"
    base = (
        request.source_span.start_offset
        if request.source_span and request.source_span.start_offset is not None
        else 0
    )
    covered = 0
    last_start = -1
    for split in splits:
        assert split.content.strip()
        assert split.token_count == counter.count(split.content)
        assert split.token_count <= request.maximum_tokens
        assert split.source_span is not None
        start = split.source_span.start_offset - base
        end = split.source_span.end_offset - base
        assert request.content[start:end] == split.content
        assert start >= last_start
        assert start <= covered or not request.content[covered:start].strip()
        covered = max(covered, end)
        last_start = start
    assert not request.content[covered:].strip()


def reconstruct(request: TextRefinementRequest, splits: tuple[TextSplit, ...]) -> str:
    """Rebuild the request content from non-overlapping splits."""

    base = (
        request.source_span.start_offset
        if request.source_span and request.source_span.start_offset is not None
        else 0
    )
    pieces: list[str] = []
    covered = 0
    for split in splits:
        assert split.source_span is not None
        start = split.source_span.start_offset - base
        end = split.source_span.end_offset - base
        pieces.append(request.content[max(start, covered) : end])
        covered = max(covered, end)
    return "".join(pieces)
