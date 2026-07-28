from __future__ import annotations

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageNotFoundError,
)
from harborrag_adapters.repositories.object_store.memory.repository import (
    MemoryObjectStore,
)
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext(tenant_id=tenant)


@pytest.mark.asyncio
async def test_put_get_head_delete_round_trip() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        reference = await store.put(
            PutObjectRequest(bucket="bkt", key="a/b.txt", body=b"hello"),
            context=context,
        )
        assert reference.size_bytes == 5
        assert await store.get_bytes("bkt", "a/b.txt", byte_range=None, context=context) == b"hello"
        metadata = await store.head("bkt", "a/b.txt", context=context)
        assert metadata.reference.checksum_sha256 == reference.checksum_sha256
        assert await store.delete("bkt", "a/b.txt", context=context) is True
        assert await store.exists("bkt", "a/b.txt", context=context) is False


@pytest.mark.asyncio
async def test_if_none_match_rejects_existing_key() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"1"), context=context)
        with pytest.raises(HarborStorageAlreadyExistsError):
            await store.put(
                PutObjectRequest(bucket="bkt", key="k", body=b"2", if_none_match=True),
                context=context,
            )


@pytest.mark.asyncio
async def test_cross_tenant_put_does_not_collide_with_existing_object() -> None:
    store = MemoryObjectStore()
    async with store:
        owner = make_context("tenant-a")
        other = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"owned"), context=owner)
        # Object identity is tenant-partitioned, so the same logical bucket/key
        # never collides across tenants; each tenant's write is independent.
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"separate"), context=other)
        assert await store.get_bytes("bkt", "k", byte_range=None, context=owner) == b"owned"
        assert await store.get_bytes("bkt", "k", byte_range=None, context=other) == b"separate"


@pytest.mark.asyncio
async def test_cross_tenant_get_is_not_found() -> None:
    store = MemoryObjectStore()
    async with store:
        owner = make_context("tenant-a")
        intruder = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"secret"), context=owner)
        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=intruder)


@pytest.mark.asyncio
async def test_list_respects_prefix_and_tenant() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="docs/a", body=b""), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="docs/b", body=b""), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="other/c", body=b""), context=context)
        results = await store.list("bkt", "docs/", limit=10, context=context)
        assert {item.reference.key for item in results} == {"docs/a", "docs/b"}


# --- Additional coverage below: capabilities, health, byte-range edge cases,
# iter_bytes chunking, bucket-scoped listing, and presign_download. ---


def test_capabilities_reports_supported_features() -> None:
    store = MemoryObjectStore()
    capabilities = store.capabilities
    assert capabilities.conditional_writes is True
    assert capabilities.range_downloads is True
    assert capabilities.streaming_upload is True
    assert capabilities.streaming_download is True


@pytest.mark.asyncio
async def test_health_reflects_connection_state() -> None:
    store = MemoryObjectStore()

    before = await store.health()
    assert before.status == HealthStatus.UNKNOWN

    async with store:
        after = await store.health()
        assert after.status == HealthStatus.HEALTHY
        assert after.details["objects"] == 0


@pytest.mark.asyncio
async def test_get_bytes_with_valid_byte_range() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"abcdefghij"), context=context
        )
        assert await store.get_bytes("bkt", "k", byte_range=(2, 5), context=context) == b"cdef"


@pytest.mark.asyncio
async def test_get_bytes_with_invalid_byte_range_raises_value_error() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)
        with pytest.raises(ValueError, match="invalid byte range"):
            await store.get_bytes("bkt", "k", byte_range=(4, 1), context=context)
        with pytest.raises(ValueError, match="invalid byte range"):
            await store.get_bytes("bkt", "k", byte_range=(-1, 2), context=context)


@pytest.mark.asyncio
async def test_iter_bytes_streams_in_chunks() -> None:
    store = MemoryObjectStore()
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
async def test_iter_bytes_missing_key_raises_not_found() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        with pytest.raises(HarborStorageNotFoundError):
            async for _ in store.iter_bytes("bkt", "missing", chunk_size=4, context=context):
                pass


@pytest.mark.asyncio
async def test_list_respects_bucket_scope_and_limit() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt-a", key="k1", body=b"1"), context=context)
        await store.put(PutObjectRequest(bucket="bkt-a", key="k2", body=b"2"), context=context)
        await store.put(PutObjectRequest(bucket="bkt-b", key="k1", body=b"3"), context=context)

        results = await store.list("bkt-a", "", limit=10, context=context)
        assert {item.reference.key for item in results} == {"k1", "k2"}

        limited = await store.list("bkt-a", "", limit=1, context=context)
        assert len(limited) == 1


@pytest.mark.asyncio
async def test_presign_download_is_not_implemented() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        with pytest.raises(NotImplementedError):
            await store.presign_download("bkt", "k", expires_seconds=60, context=context)


@pytest.mark.asyncio
async def test_delete_returns_false_when_key_absent() -> None:
    store = MemoryObjectStore()
    async with store:
        context = make_context()
        assert await store.delete("bkt", "missing", context=context) is False


@pytest.mark.asyncio
async def test_put_checksum_mismatch_raises_validation_error() -> None:
    from harborrag_adapters.repositories.errors import HarborStorageValidationError

    store = MemoryObjectStore()
    async with store:
        context = make_context()
        with pytest.raises(HarborStorageValidationError):
            await store.put(
                PutObjectRequest(bucket="bkt", key="k", body=b"data", checksum_sha256="0" * 64),
                context=context,
            )


@pytest.mark.asyncio
async def test_put_over_tampered_same_identity_metadata_is_rejected() -> None:
    """Defensive check: identity is already tenant-partitioned, so this branch
    is unreachable through normal usage; simulate a tampered stored record to
    exercise the defense-in-depth tenant_id comparison directly."""
    store = MemoryObjectStore()
    async with store:
        context = make_context("tenant-a")
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"first"), context=context)
        identity = ("tenant-a", "bkt", "k")
        tampered = store._metadata[identity]
        object.__setattr__(tampered, "metadata", {**tampered.metadata, "tenant_id": "tenant-x"})

        with pytest.raises(HarborStorageAlreadyExistsError):
            await store.put(
                PutObjectRequest(bucket="bkt", key="k", body=b"second"), context=context
            )
