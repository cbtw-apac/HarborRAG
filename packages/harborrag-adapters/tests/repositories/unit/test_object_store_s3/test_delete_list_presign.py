from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store.keys import physical_object_key
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.repository import S3ObjectStore
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext

from .fakes import ExtendedFakeS3Raw, FakeS3ClientWithLifecycle, make_extended_store


@pytest.mark.asyncio
async def test_delete_true_when_present_false_when_absent() -> None:
    context = StorageOperationContext.system(tenant_id="tenant-a")

    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    store = make_extended_store(raw)
    assert await store.delete("bucket", "key", context=context) is True
    assert len(raw.delete_calls) == 1

    raw_absent = ExtendedFakeS3Raw()
    store_absent = make_extended_store(raw_absent)
    assert await store_absent.delete("bucket", "key", context=context) is False
    assert raw_absent.delete_calls == []


@pytest.mark.asyncio
async def test_list_paginates_and_filters_foreign_tenant_and_missing_items() -> None:
    context = StorageOperationContext.system(tenant_id="tenant-a")
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})

    missing_physical = physical_object_key("tenant-a", "docs/missing")
    raw.head_by_key[missing_physical] = None
    raw.list_pages = [
        {
            "Contents": [
                {"Key": physical_object_key("tenant-a", "docs/a")},
                {"Key": missing_physical},
                {"Key": physical_object_key("tenant-b", "docs/x")},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "token-1",
        },
        {
            "Contents": [{"Key": physical_object_key("tenant-a", "docs/b")}],
            "IsTruncated": False,
        },
    ]
    store = make_extended_store(raw)

    results = await store.list("bucket", "docs/", limit=10, context=context)

    assert {item.reference.key for item in results} == {"docs/a", "docs/b"}


@pytest.mark.asyncio
async def test_presign_download_includes_version_id_when_present() -> None:
    raw = ExtendedFakeS3Raw(
        {"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"', "VersionId": "v1"}
    )
    store = make_extended_store(raw)

    url = await store.presign_download(
        "bucket",
        "key",
        expires_seconds=60,
        context=StorageOperationContext.system(tenant_id="tenant-a"),
    )

    assert url.startswith("https://example.test/bucket/")
    assert raw.presign_calls[0][1]["VersionId"] == "v1"
    assert raw.presign_calls[0][2] == 60


@pytest.mark.asyncio
async def test_presign_download_omits_version_id_when_absent() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    store = make_extended_store(raw)

    await store.presign_download(
        "bucket",
        "key",
        expires_seconds=60,
        context=StorageOperationContext.system(tenant_id="tenant-a"),
    )

    assert "VersionId" not in raw.presign_calls[0][1]


@pytest.mark.asyncio
async def test_connect_close_and_health_delegate_to_database() -> None:
    raw = ExtendedFakeS3Raw()
    client = FakeS3ClientWithLifecycle(raw)
    store = S3ObjectStore(S3ObjectStoreConfig(), client=client)  # type: ignore[arg-type]

    await store.connect()
    assert client.connected is True

    health = await store.health()
    assert health.status == HealthStatus.HEALTHY

    await store.close()
    assert client.connected is False
