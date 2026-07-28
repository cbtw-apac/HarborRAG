from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.cache.redis.config import RedisCacheConfig
from harborrag_adapters.repositories.cache.redis.repository import RedisCacheBackend
from harborrag_adapters.repositories.cache.redis.store import (
    RedisCacheStore,
    ResponseError,
    WatchError,
)
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext

CONTEXT_V2 = StorageOperationContext(tenant_id="tenant-a")


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
        self.smembers_return: set[str] | dict[str, set[str]] = (
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
        if isinstance(self.smembers_return, dict):
            return self.smembers_return.get(key, set())
        return self.smembers_return

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


class FakeBatchPipeline:
    """Fake for a non-transactional (queuing) pipeline: real redis-py never
    executes a command immediately here (only WATCH triggers that), so
    ``smembers`` just queues a key and ``execute`` resolves them all in one
    round trip, in call order."""

    def __init__(self, smembers_return: set[str] | dict[str, set[str]]) -> None:
        self._smembers_return = smembers_return
        self._queued_keys: list[str] = []

    async def __aenter__(self) -> FakeBatchPipeline:
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args

    def smembers(self, key: str) -> None:
        self._queued_keys.append(key)

    async def execute(self) -> list[set[str]]:
        if isinstance(self._smembers_return, dict):
            return [self._smembers_return.get(key, set()) for key in self._queued_keys]
        return [self._smembers_return for _ in self._queued_keys]


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

    def pipeline(self, *, transaction: bool) -> FakePipelineV2 | FakeBatchPipeline:
        if not transaction:
            # The non-transactional batch pipe used to pipeline membership
            # reads shares the CURRENT transactional pipe's smembers fixture
            # data (that pipe's slot was already claimed, hence the -1); it
            # does not itself consume a retry-loop slot.
            current_index = min(max(self._pipeline_index - 1, 0), len(self._pipelines) - 1)
            return FakeBatchPipeline(self._pipelines[current_index].smembers_return)
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
