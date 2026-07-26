from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
)
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_core.schemas.object_store import PutObjectRequest

from .conftest import make_context


@pytest.mark.asyncio
async def test_put_get_delete_round_trip(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(
            PutObjectRequest(bucket="bkt", key="a/b.txt", body=b"hello"),
            context=context,
        )
        assert await store.get_bytes("bkt", "a/b.txt", byte_range=None, context=context) == b"hello"
        assert await store.delete("bkt", "a/b.txt", context=context) is True


@pytest.mark.asyncio
async def test_object_key_cannot_escape_bucket_root(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError):
            await store.put(
                PutObjectRequest(bucket="bkt", key="../../etc/passwd", body=b"x"),
                context=context,
            )


@pytest.mark.asyncio
async def test_checksum_mismatch_leaves_no_temp_file(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(HarborStorageValidationError):
            await store.put(
                PutObjectRequest(bucket="bkt", key="k", body=b"hello", checksum_sha256="0" * 64),
                context=context,
            )
        bucket_dir = tmp_path / "bkt"
        leftover = list(bucket_dir.rglob("*.tmp")) if bucket_dir.exists() else []
        assert leftover == []


@pytest.mark.asyncio
async def test_cross_tenant_put_does_not_collide_with_existing_object(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        owner = make_context("tenant-a")
        other = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"owned"), context=owner)
        # Object paths are tenant-partitioned, so the same logical bucket/key
        # never collides across tenants; each tenant's write is independent.
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"separate"), context=other)
        assert await store.get_bytes("bkt", "k", byte_range=None, context=owner) == b"owned"
        assert await store.get_bytes("bkt", "k", byte_range=None, context=other) == b"separate"


@pytest.mark.asyncio
async def test_concurrent_if_none_match_put_has_one_winner(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def slow_body():
            first_started.set()
            await release_first.wait()
            yield b"first"

        first = asyncio.create_task(
            store.put(
                PutObjectRequest(bucket="bkt", key="atomic", body=slow_body(), if_none_match=True),
                context=context,
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            store.put(
                PutObjectRequest(bucket="bkt", key="atomic", body=b"second", if_none_match=True),
                context=context,
            )
        )
        release_first.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert sum(not isinstance(item, Exception) for item in results) == 1
        assert sum(isinstance(item, HarborStorageAlreadyExistsError) for item in results) == 1


@pytest.mark.asyncio
async def test_read_disappearance_is_mapped_to_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context)

        def disappear(_path: Path) -> bytes:
            raise FileNotFoundError

        monkeypatch.setattr(Path, "read_bytes", disappear)
        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=context)
