from __future__ import annotations

from datetime import timedelta

import pytest

from .fakes import CONTEXT_V2, FakePipelineV2, FakeRedisClientV2, make_store_v2


@pytest.mark.asyncio
async def test_get_returns_parsed_value_when_present() -> None:
    client = FakeRedisClientV2(FakePipelineV2(), get_return='{"a": 1}')
    store = make_store_v2(client)

    result = await store.get("key", context=CONTEXT_V2)

    assert result == {"a": 1}


@pytest.mark.asyncio
async def test_get_returns_none_when_absent() -> None:
    client = FakeRedisClientV2(FakePipelineV2(), get_return=None)
    store = make_store_v2(client)

    result = await store.get("key", context=CONTEXT_V2)

    assert result is None


@pytest.mark.asyncio
async def test_get_many_returns_empty_dict_for_no_keys() -> None:
    client = FakeRedisClientV2(FakePipelineV2())
    store = make_store_v2(client)

    result = await store.get_many([], context=CONTEXT_V2)

    assert result == {}


@pytest.mark.asyncio
async def test_get_many_filters_out_missing_values() -> None:
    client = FakeRedisClientV2(FakePipelineV2(), mget_return=['"one"', None, '"three"'])
    store = make_store_v2(client)

    result = await store.get_many(["a", "b", "c"], context=CONTEXT_V2)

    assert result == {"a": "one", "c": "three"}


@pytest.mark.asyncio
async def test_set_without_ttl_or_tags() -> None:
    pipeline = FakePipelineV2()
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    await store.set("key", "value", ttl=None, tags=None, context=CONTEXT_V2)

    names = [name for name, _ in pipeline.commands]
    assert names == ["multi", "delete", "set"]


@pytest.mark.asyncio
async def test_set_with_ttl_and_tags_expires_tag_index() -> None:
    pipeline = FakePipelineV2(smembers_return=set())
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    await store.set(
        "key",
        "value",
        ttl=timedelta(seconds=30),
        tags={"tag-a"},
        context=CONTEXT_V2,
    )

    names = [name for name, _ in pipeline.commands]
    assert names == ["multi", "delete", "set", "sadd", "sadd", "pexpire"]


@pytest.mark.asyncio
async def test_set_with_zero_ttl_deletes_value_and_old_tags() -> None:
    pipeline = FakePipelineV2(smembers_return={"old-tag"})
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    await store.set("key", "value", ttl=timedelta(seconds=-1), tags=None, context=CONTEXT_V2)

    names = [name for name, _ in pipeline.commands]
    assert names == ["multi", "srem", "delete", "delete"]


@pytest.mark.asyncio
async def test_set_retries_after_watch_error() -> None:
    conflicted = FakePipelineV2(watch_error=True)
    succeeded = FakePipelineV2()
    client = FakeRedisClientV2([conflicted, succeeded])
    store = make_store_v2(client)

    await store.set("key", "value", ttl=None, tags=None, context=CONTEXT_V2)

    assert [name for name, _ in succeeded.commands] == ["multi", "delete", "set"]
