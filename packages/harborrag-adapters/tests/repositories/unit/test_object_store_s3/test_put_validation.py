from __future__ import annotations

import pytest

from harborrag_adapters.repositories.errors import (
    HarborObjectTooLargeError,
    HarborStorageAlreadyExistsError,
    HarborStorageValidationError,
)
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import ExtendedFakeS3Raw, FakeClientError, HugeChunk, make_extended_store


@pytest.mark.asyncio
async def test_put_without_bucket_and_no_default_raises_validation_error() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageValidationError):
        await store.put(
            PutObjectRequest(bucket="", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_put_sets_content_type_and_server_side_encryption() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw, config=S3ObjectStoreConfig(server_side_encryption="AES256"))

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=b"value", content_type="text/plain"),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert raw.put_calls[0]["ContentType"] == "text/plain"
    assert raw.put_calls[0]["ServerSideEncryption"] == "AES256"


@pytest.mark.asyncio
async def test_put_replace_with_missing_etag_raises_already_exists() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": ""})
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageAlreadyExistsError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []


@pytest.mark.asyncio
async def test_put_checksum_mismatch_raises_validation_error() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageValidationError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value", checksum_sha256="0" * 64),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []


@pytest.mark.asyncio
async def test_put_unmapped_client_error_is_reraised() -> None:
    raw = ExtendedFakeS3Raw()
    raw.put_error = "InternalError"
    store = make_extended_store(raw)

    with pytest.raises(FakeClientError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_put_existing_head_check_reraises_unmapped_client_error() -> None:
    raw = ExtendedFakeS3Raw()
    raw.head_error = "InternalError"
    store = make_extended_store(raw)

    with pytest.raises(FakeClientError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_put_rejects_object_larger_than_five_gigabytes() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw)

    async def huge_body():
        yield HugeChunk(b"x")

    with pytest.raises(HarborObjectTooLargeError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=huge_body()),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []
