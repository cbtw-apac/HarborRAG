"""Provider-neutral token estimates stay stable across ingestion processes."""

from __future__ import annotations

from harborrag_runtime.tokenization import ApproximateTokenCounter


def test_ascii_text_uses_bpe_sized_estimates() -> None:
    counter = ApproximateTokenCounter()

    assert counter.count("") == 0
    assert counter.count("Accepted") == 2
    assert counter.count("a" * 101) == 26


def test_non_ascii_text_is_counted_conservatively() -> None:
    counter = ApproximateTokenCounter()

    assert counter.count("abcd界🙂") == 3
