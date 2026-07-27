from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_core.schemas.object_store import PutObjectRequest

from .conftest import make_context


@pytest.mark.asyncio
async def test_cross_tenant_get_is_not_found(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        owner = make_context("tenant-a")
        intruder = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"secret"), context=owner)
        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=intruder)


@pytest.mark.asyncio
async def test_delete_returns_false_when_bucket_directory_missing(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        assert await store.delete("bkt", "k", context=context) is False


@pytest.mark.asyncio
async def test_delete_returns_false_when_key_missing_but_bucket_exists(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k1", body=b"data"), context=context)
        assert await store.delete("bkt", "k2", context=context) is False


@pytest.mark.asyncio
async def test_list_respects_prefix_tenant_and_limit(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context("tenant-a")
        other = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="docs/a", body=b"1"), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="docs/b", body=b"2"), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="other/c", body=b"3"), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="docs/d", body=b"4"), context=other)

        results = await store.list("bkt", "docs/", limit=10, context=context)
        assert {item.reference.key for item in results} == {"docs/a", "docs/b"}

        limited = await store.list("bkt", "docs/", limit=1, context=context)
        assert len(limited) == 1


@pytest.mark.asyncio
async def test_list_returns_empty_when_bucket_root_missing(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        assert await store.list("bkt", "", limit=10, context=context) == []


@pytest.mark.asyncio
async def test_list_with_invalid_bucket_name_raises_value_error(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError, match="invalid filesystem bucket name"):
            await store.list("bad bucket!", "", limit=10, context=context)


@pytest.mark.asyncio
async def test_invalid_bucket_name_on_put_raises_value_error(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError, match="invalid filesystem bucket name"):
            await store.put(
                PutObjectRequest(bucket="bad bucket!", key="k", body=b"x"),
                context=context,
            )


@pytest.mark.asyncio
async def test_key_with_empty_segment_is_rejected(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError, match="invalid object key"):
            await store.put(PutObjectRequest(bucket="bkt", key="a//b", body=b"x"), context=context)


@pytest.mark.asyncio
async def test_presign_download_is_not_implemented(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(NotImplementedError):
            await store.presign_download("bkt", "k", expires_seconds=60, context=context)
