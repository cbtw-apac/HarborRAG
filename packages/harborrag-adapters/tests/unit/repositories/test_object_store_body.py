from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from harborrag_adapters.repositories.object_store.body import iter_body, read_body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [b"hello", bytearray(b"hello"), memoryview(b"hello")],
)
async def test_iter_body_bytes_like_yields_single_chunk(body: Any) -> None:
    chunks = [chunk async for chunk in iter_body(body)]
    assert chunks == [b"hello"]


@pytest.mark.asyncio
async def test_iter_body_path_reads_file_in_chunks(tmp_path: Path) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"abcdefghij")

    chunks = [chunk async for chunk in iter_body(target, chunk_size=3)]

    assert chunks == [b"abc", b"def", b"ghi", b"j"]
    assert b"".join(chunks) == b"abcdefghij"


@pytest.mark.asyncio
async def test_iter_body_async_iterable_yields_chunks_as_is() -> None:
    async def body() -> Any:
        yield b"first-"
        yield b"second"

    chunks = [chunk async for chunk in iter_body(body())]

    assert chunks == [b"first-", b"second"]


@pytest.mark.asyncio
async def test_iter_body_async_iterable_rejects_non_bytes_chunk() -> None:
    async def body() -> Any:
        yield "not-bytes"

    with pytest.raises(TypeError, match="async object body chunks must be bytes"):
        async for _ in iter_body(body()):
            pass


@pytest.mark.asyncio
async def test_iter_body_sync_file_like_reads_in_chunks() -> None:
    stream = io.BytesIO(b"abcdefghij")

    chunks = [chunk async for chunk in iter_body(stream, chunk_size=4)]

    assert chunks == [b"abcd", b"efgh", b"ij"]


@pytest.mark.asyncio
async def test_iter_body_sync_file_like_rejects_non_bytes_chunk() -> None:
    class BadFile:
        def __init__(self) -> None:
            self._read = False

        def read(self, _size: int) -> Any:
            if self._read:
                return b""
            self._read = True
            return "not-bytes"

    with pytest.raises(TypeError, match="binary object body must return bytes"):
        async for _ in iter_body(BadFile()):
            pass


@pytest.mark.asyncio
async def test_read_body_joins_bytes_chunks() -> None:
    assert await read_body(b"hello") == b"hello"


@pytest.mark.asyncio
async def test_read_body_joins_multi_chunk_stream(tmp_path: Path) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"the-quick-brown-fox")

    async def body() -> Any:
        yield b"the-quick-"
        yield b"brown-fox"

    assert await read_body(body()) == b"the-quick-brown-fox"
