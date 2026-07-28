from __future__ import annotations

import pytest

from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import ExtendedFakeS3Raw, FakeClientError, make_extended_store


@pytest.mark.asyncio
async def test_get_bytes_without_range_reads_entire_object() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    raw.object_body = b"hello world"
    store = make_extended_store(raw)

    data = await store.get_bytes(
        "bucket",
        "key",
        byte_range=None,
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert data == b"hello world"
    assert "Range" not in raw.get_object_calls[0]


@pytest.mark.asyncio
async def test_get_bytes_with_byte_range_sets_range_header() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    raw.object_body = b"hello world"
    store = make_extended_store(raw)

    await store.get_bytes(
        "bucket",
        "key",
        byte_range=(0, 4),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert raw.get_object_calls[0]["Range"] == "bytes=0-4"


@pytest.mark.asyncio
async def test_iter_bytes_streams_in_chunks() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    raw.object_body = b"abcdefghij"
    store = make_extended_store(raw)

    chunks = [
        chunk
        async for chunk in store.iter_bytes(
            "bucket",
            "key",
            chunk_size=4,
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    ]

    assert chunks == [b"abcd", b"efgh", b"ij"]


@pytest.mark.asyncio
async def test_head_returns_normalized_metadata() -> None:
    raw = ExtendedFakeS3Raw(
        {
            "Metadata": {"tenant_id": "tenant-a", "custom": "1"},
            "ETag": '"etag-value"',
            "ContentLength": 42,
            "ContentType": "text/plain",
        }
    )
    store = make_extended_store(raw)

    metadata = await store.head(
        "bucket", "key", context=StorageOperationContext(tenant_id="tenant-a")
    )

    assert metadata.reference.etag == "etag-value"
    assert metadata.reference.size_bytes == 42
    assert metadata.reference.content_type == "text/plain"
    assert metadata.metadata["custom"] == "1"


@pytest.mark.asyncio
async def test_exists_true_for_owned_object_false_otherwise() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")

    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    store = make_extended_store(raw)
    assert await store.exists("bucket", "key", context=context) is True

    raw_absent = ExtendedFakeS3Raw()
    store_absent = make_extended_store(raw_absent)
    assert await store_absent.exists("bucket", "key", context=context) is False


@pytest.mark.asyncio
async def test_authorize_unmapped_client_error_is_reraised() -> None:
    raw = ExtendedFakeS3Raw()
    raw.head_error = "InternalError"
    store = make_extended_store(raw)

    with pytest.raises(FakeClientError):
        await store.exists("bucket", "key", context=StorageOperationContext(tenant_id="tenant-a"))


@pytest.mark.asyncio
async def test_authorize_cross_tenant_object_is_not_found() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-b"}, "ETag": '"e"'})
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageNotFoundError):
        await store.get_bytes(
            "bucket",
            "key",
            byte_range=None,
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
