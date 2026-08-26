from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO

from harborrag_core.schemas.object_store import ObjectBody


async def iter_body(body: ObjectBody, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    if isinstance(body, (bytes, bytearray, memoryview)):
        yield bytes(body)
        return
    if isinstance(body, Path):
        file = await asyncio.to_thread(body.open, "rb")
        try:
            while chunk := await asyncio.to_thread(file.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(file.close)
        return
    if hasattr(body, "__aiter__"):
        async for chunk in body:
            if not isinstance(chunk, bytes):
                raise TypeError("async object body chunks must be bytes")
            yield chunk
        return
    file_like: BinaryIO = body
    while chunk := await asyncio.to_thread(file_like.read, chunk_size):
        if not isinstance(chunk, bytes):
            raise TypeError("binary object body must return bytes")
        yield chunk


async def read_body(body: ObjectBody) -> bytes:
    chunks = [chunk async for chunk in iter_body(body)]
    return b"".join(chunks)
