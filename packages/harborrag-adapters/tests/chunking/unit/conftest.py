from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from harborrag_core.contracts.chunking import TextRefinementRequest, TextSplit


class WordCounter:
    """Count whitespace-separated words: deterministic and easy to reason about."""

    name = "words"
    version = "1"

    def count(self, text: str) -> int:
        return len(text.split())


@pytest.fixture
def words() -> WordCounter:
    return WordCounter()


def _assert_tiles(request: TextRefinementRequest, splits: Sequence[TextSplit]) -> None:
    """Every split fits, maps back to its source range, and together they cover it."""

    assert splits
    base = request.source_span.start_offset if request.source_span else 0
    base = base or 0
    covered = 0
    for split in splits:
        assert split.token_count <= request.maximum_tokens
        span = split.source_span
        assert span is not None and span.start_offset is not None and span.end_offset is not None
        start, end = span.start_offset - base, span.end_offset - base
        assert request.content[start:end] == split.content
        assert start <= covered, "splits left a gap in the source"
        covered = max(covered, end)
    assert covered == len(request.content)


@pytest.fixture
def assert_tiles() -> Callable[[TextRefinementRequest, Sequence[TextSplit]], None]:
    return _assert_tiles
