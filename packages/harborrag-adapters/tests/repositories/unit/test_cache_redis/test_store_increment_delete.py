from __future__ import annotations

from datetime import timedelta

import pytest

from harborrag_adapters.repositories.cache.redis.store import RedisCacheStore

from .fakes import CONTEXT_V2, FakePipelineV2, FakeRedisClientV2, make_store_v2


@pytest.mark.asyncio
async def test_increment_returns_new_integer_value() -> None:
    pipeline = FakePipelineV2(execute_return=[5])
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    result = await store.increment("counter", 1, ttl=None, context=CONTEXT_V2)

    assert result == 5
    assert [name for name, _ in pipeline.commands] == ["incrby"]


@pytest.mark.asyncio
async def test_increment_with_ttl_queues_pexpire() -> None:
    pipeline = FakePipelineV2(execute_return=[5, 1])
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    result = await store.increment("counter", 1, ttl=timedelta(seconds=30), context=CONTEXT_V2)

    assert result == 5
    assert [name for name, _ in pipeline.commands] == ["incrby", "pexpire"]


@pytest.mark.asyncio
async def test_increment_raises_type_error_for_non_integer_value() -> None:
    pipeline = FakePipelineV2(response_error=True)
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    with pytest.raises(TypeError, match="cache increment requires an integer value"):
        await store.increment("counter", 1, ttl=None, context=CONTEXT_V2)


@pytest.mark.asyncio
async def test_delete_removes_value_and_tag_memberships() -> None:
    pipeline = FakePipelineV2(execute_return=[0, 0, 1])
    client = FakeRedisClientV2(pipeline, smembers_return={"tenant-a:tags:key": {"tag-a"}})
    store = make_store_v2(client)

    deleted = await store.delete("key", context=CONTEXT_V2)

    assert deleted is True
    assert [name for name, _ in pipeline.commands] == ["srem", "delete", "delete"]


@pytest.mark.asyncio
async def test_delete_returns_false_when_value_absent() -> None:
    pipeline = FakePipelineV2(execute_return=[0])
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    deleted = await store.delete("key", context=CONTEXT_V2)

    assert deleted is False


def test_ttl_milliseconds_static_conversion() -> None:
    assert RedisCacheStore._ttl_milliseconds(None) is None
    assert RedisCacheStore._ttl_milliseconds(timedelta(seconds=-1)) == 0
    assert RedisCacheStore._ttl_milliseconds(timedelta(milliseconds=0.5)) == 1
    assert RedisCacheStore._ttl_milliseconds(timedelta(seconds=1)) == 1000
