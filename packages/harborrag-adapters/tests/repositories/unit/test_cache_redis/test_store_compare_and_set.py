from __future__ import annotations

from datetime import timedelta

import pytest

from harborrag_adapters.repositories.cache.redis.store import RedisCacheStore
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import (
    CONTEXT_V2,
    FakeBackend,
    FakePipeline,
    FakePipelineV2,
    FakeRedisClientV2,
    make_store_v2,
)


@pytest.mark.asyncio
async def test_compare_and_set_preserves_redis_tag_index() -> None:
    pipeline = FakePipeline()
    store = RedisCacheStore(FakeBackend(pipeline))  # type: ignore[arg-type]

    replaced = await store.compare_and_set(
        "key",
        "old",
        "new",
        ttl=None,
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert replaced is True
    assert [name for name, _ in pipeline.commands] == ["set", "persist"]


@pytest.mark.asyncio
async def test_compare_and_set_returns_false_on_mismatch() -> None:
    pipeline = FakePipelineV2(get_return='"other"')
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    replaced = await store.compare_and_set("key", "expected", "value", ttl=None, context=CONTEXT_V2)

    assert replaced is False
    assert pipeline.commands == []


@pytest.mark.asyncio
async def test_compare_and_set_with_zero_ttl_deletes_value() -> None:
    pipeline = FakePipelineV2(get_return='"old"', smembers_return={"tag-a"})
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    replaced = await store.compare_and_set(
        "key", "old", "new", ttl=timedelta(seconds=-1), context=CONTEXT_V2
    )

    assert replaced is True
    names = [name for name, _ in pipeline.commands]
    assert names == ["multi", "srem", "delete", "delete"]


@pytest.mark.asyncio
async def test_compare_and_set_with_ttl_and_old_tags_uses_pexpire() -> None:
    pipeline = FakePipelineV2(get_return='"old"', smembers_return={"tag-a"})
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    replaced = await store.compare_and_set(
        "key", "old", "new", ttl=timedelta(seconds=30), context=CONTEXT_V2
    )

    assert replaced is True
    names = [name for name, _ in pipeline.commands]
    assert names == ["multi", "set", "pexpire"]


@pytest.mark.asyncio
async def test_compare_and_set_returns_false_on_watch_error() -> None:
    pipeline = FakePipelineV2(get_return='"old"', watch_error=True)
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    replaced = await store.compare_and_set("key", "old", "new", ttl=None, context=CONTEXT_V2)

    assert replaced is False
