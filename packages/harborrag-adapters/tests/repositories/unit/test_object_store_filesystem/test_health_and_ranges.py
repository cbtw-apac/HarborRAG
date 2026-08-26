from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageNotFoundError,
    HarborStorageValidationError,
)
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.storage import HealthStatus

from .conftest import make_context


def test_missing_root_and_config_raises_value_error() -> None:
    with pytest.raises(ValueError, match="root is required"):
        FilesystemObjectStore()


@pytest.mark.asyncio
async def test_health_reflects_connection_state(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)

    before = await store.health()
    assert before.status == HealthStatus.UNHEALTHY

    async with store:
        after = await store.health()
        assert after.status == HealthStatus.HEALTHY
        assert after.details["writable"] is True


@pytest.mark.asyncio
async def test_get_bytes_with_valid_byte_range(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"abcdefghij"), context=context
        )
        data = await store.get_bytes("bkt", "k", byte_range=(2, 5), context=context)
        assert data == b"cdef"


@pytest.mark.asyncio
async def test_get_bytes_with_invalid_byte_range_raises_validation_error(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)
        with pytest.raises(HarborStorageValidationError):
            await store.get_bytes("bkt", "k", byte_range=(5, 1), context=context)
        with pytest.raises(HarborStorageValidationError):
            await store.get_bytes("bkt", "k", byte_range=(-1, 2), context=context)


@pytest.mark.asyncio
async def test_iter_bytes_streams_in_chunks(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"abcdefghij"), context=context
        )
        chunks = [
            chunk async for chunk in store.iter_bytes("bkt", "k", chunk_size=4, context=context)
        ]
        assert chunks == [b"abcd", b"efgh", b"ij"]


@pytest.mark.asyncio
async def test_iter_bytes_missing_key_raises_not_found(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(HarborStorageNotFoundError):
            async for _ in store.iter_bytes("bkt", "missing", chunk_size=4, context=context):
                pass


@pytest.mark.asyncio
async def test_head_returns_metadata(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        reference = await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context
        )
        metadata = await store.head("bkt", "k", context=context)
        assert metadata.reference.checksum_sha256 == reference.checksum_sha256


@pytest.mark.asyncio
async def test_exists_true_and_false(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        assert await store.exists("bkt", "k", context=context) is False
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context)
        assert await store.exists("bkt", "k", context=context) is True
