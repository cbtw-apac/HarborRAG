from __future__ import annotations

from typing import Any

import pytest
from harborrag_adapters.repositories.cache.redis.store import RedisCacheStore
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext


class FakePipeline:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args

    async def watch(self, *keys: str) -> None:
        del keys

    async def get(self, key: str) -> str:
        del key
        return '"old"'

    async def smembers(self, key: str) -> set[str]:
        del key
        return {"group"}

    def multi(self) -> None:
        pass

    def set(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("set", (*args, kwargs)))

    def persist(self, *args: Any) -> None:
        self.commands.append(("persist", args))

    def expire(self, *args: Any) -> None:
        self.commands.append(("expire", args))

    def srem(self, *args: Any) -> None:
        self.commands.append(("srem", args))

    def delete(self, *args: Any) -> None:
        self.commands.append(("delete", args))

    async def execute(self) -> list[int]:
        return [1]


class FakeRedisClient:
    def __init__(self, pipeline: FakePipeline) -> None:
        self._pipeline = pipeline

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert transaction is True
        return self._pipeline


class FakeBackend:
    def __init__(self, pipeline: FakePipeline) -> None:
        self.client = FakeRedisClient(pipeline)
        self._telemetry = RepositoryTelemetry(
            None,
            family=StorageFamily.CACHE,
            backend="redis",
        )

    @staticmethod
    def value_key(context: StorageOperationContext, key: str) -> str:
        return f"{context.tenant_id}:value:{key}"

    @staticmethod
    def tags_key(context: StorageOperationContext, key: str) -> str:
        return f"{context.tenant_id}:tags:{key}"

    @staticmethod
    def tag_key(context: StorageOperationContext, tag: str) -> str:
        return f"{context.tenant_id}:tag:{tag}"


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


# ---------------------------------------------------------------------------
# Additional coverage: remaining RedisCacheStore branches, plus RedisCacheBackend
# (cache/redis/repository.py). New fakes are namespaced with a "V2" suffix so the
# original FakePipeline/FakeRedisClient/FakeBackend above stay untouched.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from harborrag_adapters.repositories.cache.redis.config import RedisCacheConfig  # noqa: E402
from harborrag_adapters.repositories.cache.redis.repository import RedisCacheBackend  # noqa: E402
from harborrag_core.schemas.storage import HealthStatus  # noqa: E402
from redis.exceptions import ResponseError, WatchError  # noqa: E402

CONTEXT_V2 = StorageOperationContext(tenant_id="tenant-a")


class FakePipelineV2:
    """Configurable pipeline fake supporting the full RedisCacheStore command set."""

    def __init__(
        self,
        *,
        get_return: str | None = None,
        smembers_return: set[str] | dict[str, set[str]] | None = None,
        execute_return: list[Any] | None = None,
        watch_error: bool = False,
        response_error: bool = False,
    ) -> None:
        self.commands: list[tuple[str, tuple[Any, ...]]] = []
        self._get_return = get_return
        self._smembers_return: set[str] | dict[str, set[str]] = (
            smembers_return if smembers_return is not None else set()
        )
        self._execute_return = execute_return if execute_return is not None else [1]
        self._watch_error = watch_error
        self._response_error = response_error
        self.watched: list[str] = []

    async def __aenter__(self) -> FakePipelineV2:
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args

    async def watch(self, *keys: str) -> None:
        self.watched.extend(keys)

    async def get(self, key: str) -> str | None:
        del key
        return self._get_return

    async def smembers(self, key: str) -> set[str]:
        if isinstance(self._smembers_return, dict):
            return self._smembers_return.get(key, set())
        return self._smembers_return

    def multi(self) -> None:
        self.commands.append(("multi", ()))

    def set(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("set", (*args, kwargs)))

    def persist(self, *args: Any) -> None:
        self.commands.append(("persist", args))

    def pexpire(self, *args: Any) -> None:
        self.commands.append(("pexpire", args))

    def srem(self, *args: Any) -> None:
        self.commands.append(("srem", args))

    def sadd(self, *args: Any) -> None:
        self.commands.append(("sadd", args))

    def delete(self, *args: Any) -> None:
        self.commands.append(("delete", args))

    def incrby(self, *args: Any) -> None:
        self.commands.append(("incrby", args))

    async def execute(self) -> list[Any]:
        if self._watch_error:
            raise WatchError("conflict")
        if self._response_error:
            raise ResponseError("value is not an integer or out of range")
        return self._execute_return


class FakeRedisClientV2:
    def __init__(
        self,
        pipelines: FakePipelineV2 | list[FakePipelineV2],
        *,
        get_return: str | None = None,
        mget_return: list[str | None] | None = None,
        smembers_return: dict[str, set[str]] | None = None,
    ) -> None:
        self._pipelines = pipelines if isinstance(pipelines, list) else [pipelines]
        self._pipeline_index = 0
        self.get_return = get_return
        self.mget_return = mget_return or []
        self._smembers_return = smembers_return or {}
        self.smembers_calls: list[str] = []

    def pipeline(self, *, transaction: bool) -> FakePipelineV2:
        assert transaction is True
        index = min(self._pipeline_index, len(self._pipelines) - 1)
        self._pipeline_index += 1
        return self._pipelines[index]

    async def get(self, key: str) -> str | None:
        del key
        return self.get_return

    async def mget(self, keys: list[str]) -> list[str | None]:
        del keys
        return self.mget_return

    async def smembers(self, key: str) -> set[str]:
        self.smembers_calls.append(key)
        return self._smembers_return.get(key, set())


class FakeBackendV2:
    def __init__(self, client: FakeRedisClientV2) -> None:
        self.client = client
        self._telemetry = RepositoryTelemetry(
            None,
            family=StorageFamily.CACHE,
            backend="redis",
        )

    @staticmethod
    def value_key(context: StorageOperationContext, key: str) -> str:
        return f"{context.tenant_id}:value:{key}"

    @staticmethod
    def tags_key(context: StorageOperationContext, key: str) -> str:
        return f"{context.tenant_id}:tags:{key}"

    @staticmethod
    def tags_key_for_value_key(value_key: str) -> str:
        return value_key.replace(":value:", ":tags:", 1)

    @staticmethod
    def tag_key(context: StorageOperationContext, tag: str) -> str:
        return f"{context.tenant_id}:tag:{tag}"


def make_store_v2(client: FakeRedisClientV2) -> RedisCacheStore:
    return RedisCacheStore(FakeBackendV2(client))  # type: ignore[arg-type]


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


@pytest.mark.asyncio
async def test_invalidate_tag_returns_zero_when_no_members() -> None:
    client = FakeRedisClientV2(FakePipelineV2())
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 0


@pytest.mark.asyncio
async def test_invalidate_tag_deletes_all_tagged_values() -> None:
    pipeline = FakePipelineV2(
        execute_return=[1, 1, 1, 1],
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1"},
            "tenant-a:tags:key-1": {"tag-a"},
        },
    )
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 1
    names = [name for name, _ in pipeline.commands]
    assert names == ["multi", "srem", "delete", "delete", "delete"]
    # Both the tag index and every tagged value's own tag-membership key must
    # be watched, so a concurrent re-tag between the read and EXEC aborts the
    # transaction instead of invalidating against stale membership data.
    assert set(pipeline.watched) == {"tenant-a:tag:tag-a", "tenant-a:tags:key-1"}


@pytest.mark.asyncio
async def test_invalidate_tag_retries_after_concurrent_retag_watch_error() -> None:
    conflicted = FakePipelineV2(
        watch_error=True,
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1"},
            "tenant-a:tags:key-1": {"tag-a"},
        },
    )
    succeeded = FakePipelineV2(
        execute_return=[1, 1, 1, 1],
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1"},
            "tenant-a:tags:key-1": {"tag-a"},
        },
    )
    client = FakeRedisClientV2([conflicted, succeeded])
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 1
    assert [name for name, _ in succeeded.commands] == ["multi", "srem", "delete", "delete", "delete"]


def test_ttl_milliseconds_static_conversion() -> None:
    assert RedisCacheStore._ttl_milliseconds(None) is None
    assert RedisCacheStore._ttl_milliseconds(timedelta(seconds=-1)) == 0
    assert RedisCacheStore._ttl_milliseconds(timedelta(milliseconds=0.5)) == 1
    assert RedisCacheStore._ttl_milliseconds(timedelta(seconds=1)) == 1000


# ---------------------------------------------------------------------------
# RedisCacheBackend (cache/redis/repository.py)
# ---------------------------------------------------------------------------


class FakeDatabaseClientV2:
    def __init__(self, *, connected: bool = False, ping_error: Exception | None = None) -> None:
        self._connected = connected
        self.connect_calls = 0
        self.close_calls = 0
        self.ping_calls = 0
        self._ping_error = ping_error

    @property
    def backend(self) -> str:
        return "redis"

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def raw(self) -> Any:
        if not self._connected:
            raise RuntimeError("Redis database client is not connected")
        return self

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False

    async def ping(self) -> None:
        self.ping_calls += 1
        if self._ping_error is not None:
            raise self._ping_error


def make_cache_backend(fake_client: FakeDatabaseClientV2) -> RedisCacheBackend:
    config = RedisCacheConfig(url="redis://localhost:6379/0", instance_name="primary")
    return RedisCacheBackend(config, client=fake_client)  # type: ignore[arg-type]


def test_cache_backend_client_property_delegates_to_database_raw() -> None:
    fake_client = FakeDatabaseClientV2(connected=True)
    backend = make_cache_backend(fake_client)

    assert backend.client is fake_client


@pytest.mark.asyncio
async def test_cache_backend_connect_and_close_delegate() -> None:
    fake_client = FakeDatabaseClientV2(connected=False)
    backend = make_cache_backend(fake_client)

    await backend.connect()
    assert fake_client.connect_calls == 1

    await backend.close()
    assert fake_client.close_calls == 1


@pytest.mark.asyncio
async def test_cache_backend_health_unknown_when_not_connected() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2(connected=False))

    health = await backend.health()

    assert health.status == HealthStatus.UNKNOWN
    assert health.instance_name == "primary"


@pytest.mark.asyncio
async def test_cache_backend_health_healthy_when_connected() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2(connected=True))

    health = await backend.health()

    assert health.status == HealthStatus.HEALTHY


def test_cache_backend_key_helpers_build_prefixed_and_escaped_keys() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2())

    assert backend.value_key(CONTEXT_V2, "k:1") == "harborrag:v1:tenant-a:cache:k%3A1"
    assert backend.tags_key(CONTEXT_V2, "k:1") == "harborrag:v1:tenant-a:cache_tags:k%3A1"
    assert backend.tags_key_for_value_key(backend.value_key(CONTEXT_V2, "k")) == (
        backend.tags_key(CONTEXT_V2, "k")
    )
    assert backend.tag_key(CONTEXT_V2, "t:1") == "harborrag:v1:tenant-a:tag:t%3A1"
    assert backend.lock_key(CONTEXT_V2, "l:1") == "harborrag:v1:tenant-a:lock:l%3A1"
    assert backend.fencing_key(CONTEXT_V2, "l:1") == "harborrag:v1:tenant-a:fence:l%3A1"


def test_cache_backend_error_context_includes_operation_and_resource() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2())

    error_context = backend.error_context("get", "resource-1")

    assert error_context.operation == "get"
    assert error_context.resource_name == "resource-1"
    assert error_context.instance_name == "primary"
    assert error_context.family == StorageFamily.CACHE
