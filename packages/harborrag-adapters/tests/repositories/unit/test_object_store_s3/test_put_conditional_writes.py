from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.errors import HarborStorageAlreadyExistsError
from harborrag_adapters.repositories.object_store.s3 import (
    operations as operations_module,
)
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import FakeS3Raw, make_store


@pytest.mark.asyncio
async def test_absent_key_always_uses_atomic_create_condition() -> None:
    raw = FakeS3Raw()
    store = make_store(raw)

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=b"value"),
        context=StorageOperationContext.system(tenant_id="tenant-a"),
    )

    assert raw.put_calls[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in raw.put_calls[0]


@pytest.mark.asyncio
async def test_same_tenant_replacement_uses_observed_etag() -> None:
    raw = FakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"old-etag"'})
    store = make_store(raw)

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=b"replacement"),
        context=StorageOperationContext.system(tenant_id="tenant-a"),
    )

    assert raw.put_calls[0]["IfMatch"] == '"old-etag"'
    assert "IfNoneMatch" not in raw.put_calls[0]


@pytest.mark.asyncio
async def test_async_body_uses_single_conditional_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = FakeS3Raw()
    store = make_store(raw)

    async def immediate(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(operations_module.asyncio, "to_thread", immediate)

    async def body():
        yield b"streamed-"
        yield b"value"

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=body()),
        context=StorageOperationContext.system(tenant_id="tenant-a"),
    )

    assert len(raw.put_calls) == 1
    assert raw.put_calls[0]["IfNoneMatch"] == "*"
    assert raw.uploaded_body == b"streamed-value"
    assert store.capabilities.multipart_upload is False


@pytest.mark.asyncio
async def test_failed_write_condition_is_mapped_to_already_exists() -> None:
    raw = FakeS3Raw()
    raw.put_error = "PreconditionFailed"
    store = make_store(raw)

    with pytest.raises(HarborStorageAlreadyExistsError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext.system(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_cross_tenant_object_is_never_sent_to_put_object() -> None:
    raw = FakeS3Raw({"Metadata": {"tenant_id": "tenant-b"}, "ETag": '"old-etag"'})
    store = make_store(raw)

    with pytest.raises(HarborStorageAlreadyExistsError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext.system(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []
