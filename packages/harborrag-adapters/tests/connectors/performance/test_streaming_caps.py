"""Streaming cap tests for connector response bodies."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from harbor_test_builders import FakeResponse

from harborrag_adapters.connectors.policies.http import (
    ResponseTooLargeError,
    read_capped_content,
)

pytestmark = [pytest.mark.slow, pytest.mark.blackbox, pytest.mark.timeout(30)]


class _CountingResponse(FakeResponse):
    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        _ = chunk_size
        self.consumed = 0
        for chunk in self._chunks:
            self.consumed += len(chunk)
            yield chunk


def test_read_capped_content_aborts_early_without_buffering_everything() -> None:
    chunk = b"x" * 1024
    total_chunks = 100
    cap = 5_000
    response = _CountingResponse(_chunks=[chunk] * total_chunks)

    with pytest.raises(ResponseTooLargeError):
        read_capped_content(response, cap)

    total_available = len(chunk) * total_chunks
    assert response.consumed < total_available
    assert response.consumed <= cap + len(chunk)
    assert response.closed is True


def test_read_capped_content_rejects_oversized_content_length_upfront() -> None:
    chunk = b"x" * 1024
    response = _CountingResponse(
        headers={"Content-Length": "999999"},
        _chunks=[chunk] * 50,
    )

    with pytest.raises(ResponseTooLargeError, match="Content-Length"):
        read_capped_content(response, 1_000)

    assert response.closed is True
    assert not hasattr(response, "consumed")


def test_read_capped_content_rejects_unparseable_content_length_via_stream() -> None:
    chunk = b"x" * 1024
    response = _CountingResponse(
        headers={"Content-Length": "not-a-number"},
        _chunks=[chunk] * 50,
    )

    with pytest.raises(ResponseTooLargeError, match="Downloaded body"):
        read_capped_content(response, 1_000)

    assert response.closed is True
    assert response.consumed <= 1_000 + len(chunk)
    assert response.consumed < len(chunk) * 50


def test_read_capped_content_returns_body_within_cap() -> None:
    response = _CountingResponse(_chunks=[b"abc", b"def", b"ghi"])
    body = read_capped_content(response, 1_000)
    assert body == b"abcdefghi"
