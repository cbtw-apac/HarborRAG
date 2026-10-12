from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence

import pytest

from harborrag_adapters.sources._kit.bisect import bisect_batch
from harborrag_adapters.sources._kit.blobs import BlobPolicy, HashingTee, capped_chunks
from harborrag_adapters.sources._kit.errors import ResponseTooLarge, UnexpectedStatus
from harborrag_adapters.sources._kit.pagination import (
    Page,
    iterate_pages,
    next_link,
    offset_token,
    parse_offset,
)
from harborrag_core.sources import AuthError, DocKey, ItemFailure, Transient

ITEMS = list(range(23))


async def _offset_page(token: str | None) -> Page[int]:
    offset = parse_offset(token)
    chunk = ITEMS[offset : offset + 5]
    following = offset + len(chunk)
    return Page(chunk, offset_token(following) if following < len(ITEMS) else None)


async def _drain(start: str | None) -> list[int]:
    return [item async for page in iterate_pages(_offset_page, start) for item in page.items]


@pytest.mark.asyncio
async def test_resuming_from_any_token_yields_exactly_the_rest() -> None:
    tokens = [None] + [page.next_token async for page in iterate_pages(_offset_page)][:-1]
    for token in tokens:
        assert await _drain(token) == ITEMS[parse_offset(token) :]


@pytest.mark.asyncio
async def test_a_repeated_token_or_endless_source_stops_with_transient() -> None:
    async def stuck(token: str | None) -> Page[int]:
        return Page([1], "same")

    with pytest.raises(Transient, match="repeated"):
        _ = [page async for page in iterate_pages(stuck)]

    async def endless(token: str | None) -> Page[int]:
        return Page([1], str(parse_offset(token) + 1))

    with pytest.raises(Transient, match="exceeded 3"):
        _ = [page async for page in iterate_pages(endless, max_pages=3)]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (
            '<https://api.github.com/x?page=2>; rel="next", <https://a/last>; rel="last"',
            "https://api.github.com/x?page=2",
        ),
        ('<https://a/prev>; rel="prev", <https://a/n>; rel=next', "https://a/n"),
        ('<https://a/first>; rel="first"', None),
        ("", None),
    ],
)
def test_next_link_reads_rfc8288_headers(header: str, expected: str | None) -> None:
    assert next_link({"link": header} if header else {}) == expected


def test_offset_tokens_validate() -> None:
    with pytest.raises(ValueError):
        parse_offset("abc")
    with pytest.raises(ValueError):
        offset_token(-1)


def _key(item: int) -> DocKey:
    return DocKey("conn", "jira_issue", str(item))


class _BulkFetch:
    def __init__(self, bad: set[int]) -> None:
        self.bad = bad
        self.calls = 0

    async def __call__(self, batch: Sequence[int]) -> list[str]:
        self.calls += 1
        if self.bad & set(batch):
            raise UnexpectedStatus(400, "cannot decode issue")
        return [f"issue-{item}" for item in batch]


async def _bisect(items: list[int], fetch: _BulkFetch) -> list[str | ItemFailure]:
    return [result async for result in bisect_batch(items, fetch, key_of=_key)]


@pytest.mark.asyncio
async def test_one_bad_item_costs_one_failure_and_logarithmic_calls() -> None:
    fetch = _BulkFetch({37})
    results = await _bisect(list(range(64)), fetch)
    failures = [result for result in results if isinstance(result, ItemFailure)]
    assert [failure.key for failure in failures] == [_key(37)]
    assert [result for result in results if isinstance(result, str)] == [
        f"issue-{item}" for item in range(64) if item != 37
    ]
    assert fetch.calls <= 1 + 2 * 6


@pytest.mark.asyncio
async def test_an_all_bad_batch_costs_n_failures_and_linear_calls() -> None:
    fetch = _BulkFetch(set(range(16)))
    results = await _bisect(list(range(16)), fetch)
    assert len(results) == 16 and all(isinstance(r, ItemFailure) for r in results)
    assert fetch.calls == 2 * 16 - 1


@pytest.mark.asyncio
async def test_run_level_errors_are_not_bisected() -> None:
    calls = 0

    async def fetch(batch: Sequence[int]) -> list[int]:
        nonlocal calls
        calls += 1
        raise AuthError("revoked")

    with pytest.raises(AuthError):
        _ = [r async for r in bisect_batch([1, 2, 3, 4], fetch, key_of=_key)]
    assert calls == 1
    assert [r async for r in bisect_batch([], fetch, key_of=_key)] == []


def test_blob_policy_skips_by_type_media_and_size() -> None:
    key = _key(1)
    policy = BlobPolicy.of(["pdf", ".DOCX"], max_megabytes=1, media_types={"application/pdf"})
    assert policy.admit(key, "cv.PDF", size=10, media_type="application/pdf; charset=x") is None
    rejected = policy.admit(key, "photo.png", size=10)
    assert rejected is not None and rejected.reason == "unsupported_type" and rejected.skipped
    assert policy.admit(key, "cv.pdf", media_type="image/png").reason == "unsupported_type"  # type: ignore[union-attr]
    too_large = policy.admit(key, "cv.docx", size=2 * 1024 * 1024)
    assert too_large is not None and too_large.reason == "too_large" and not too_large.retryable


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


@pytest.mark.asyncio
async def test_capped_stream_stops_mid_stream_and_hash_matches() -> None:
    seen: list[bytes] = []
    with pytest.raises(ResponseTooLarge):
        async for chunk in capped_chunks(_chunks(b"a" * 4, b"b" * 4, b"c" * 4), 10):
            seen.append(chunk)
    assert seen == [b"a" * 4, b"b" * 4]

    tee = HashingTee()
    body = b"".join([chunk async for chunk in tee.tee(_chunks(b"hello ", b"world"))])
    assert tee.hexdigest == hashlib.sha256(body).hexdigest()
    assert tee.size == 11
