from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from harborrag_adapters.models.common.cache_redis import (
    PydanticResponseCodec,
    RedisModelCache,
)
from harborrag_adapters.models.common.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.common.redis_config import RedisConnectionConfig
from harborrag_adapters.models.common.routing_state import (
    RoutingAdmissionError,
    RoutingLease,
    RoutingStateSnapshot,
)
from harborrag_adapters.models.common.routing_state_memory import (
    InMemoryRoutingStateStore,
)
from harborrag_adapters.models.common.routing_state_redis import RedisRoutingStateStore
from pydantic import BaseModel


class Payload(BaseModel):
    value: str


class SyncRedisFake:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.hashes: dict[str, dict[Any, Any]] = {}
        self.eval_results: list[Any] = []
        self.eval_calls: list[tuple[Any, ...]] = []
        self.closed = 0

    def get(self, name: str) -> Any:
        return self.values.get(name)

    def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        if kwargs.get("nx") and name in self.values:
            return False
        self.values[name] = value
        return True

    def delete(self, *names: str) -> int:
        return sum(self.values.pop(name, None) is not None for name in names)

    def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        self.eval_calls.append((script, numkeys, *args))
        return self.eval_results.pop(0) if self.eval_results else 1

    def hgetall(self, name: str) -> Any:
        return self.hashes.get(name, {})

    def close(self) -> None:
        self.closed += 1


class AsyncRedisFake:
    def __init__(self, sync: SyncRedisFake) -> None:
        self.sync = sync
        self.closed = 0

    async def get(self, name: str) -> Any:
        return self.sync.get(name)

    async def set(self, name: str, value: Any, **kwargs: Any) -> Any:
        return self.sync.set(name, value, **kwargs)

    async def delete(self, *names: str) -> Any:
        return self.sync.delete(*names)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        return self.sync.eval(script, numkeys, *args)

    async def hgetall(self, name: str) -> Any:
        return self.sync.hgetall(name)

    async def aclose(self) -> None:
        self.closed += 1


def lifecycle(
    *, owns: bool = False
) -> tuple[RedisConnectionLifecycle, SyncRedisFake, AsyncRedisFake]:
    sync = SyncRedisFake()
    async_client = AsyncRedisFake(sync)
    connections = RedisConnectionLifecycle(
        RedisConnectionConfig(url="redis://localhost/0"),
        sync_client=sync,
        async_client=async_client,
        owns_clients=owns,
    )
    return connections, sync, async_client


def test_redis_config_and_connection_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=r"redis.url"):
        RedisConnectionConfig(url="http://localhost")
    config = RedisConnectionConfig(url="rediss://user:secret@example/0")
    assert config.resolved_url().startswith("rediss://")
    assert "secret" not in repr(config)

    connections, sync, async_client = lifecycle(owns=True)
    assert connections.sync() is sync
    assert connections.async_client() is async_client
    connections.close()
    connections.close()
    assert sync.closed == async_client.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        connections.sync()

    built_sync = SyncRedisFake()
    built_async = AsyncRedisFake(built_sync)

    class RedisFactory:
        @staticmethod
        def from_url(url: str, **kwargs: Any) -> SyncRedisFake:
            assert url == "redis://lazy/0"
            assert kwargs["decode_responses"] is True
            return built_sync

    class AsyncRedisFactory:
        @staticmethod
        def from_url(url: str, **kwargs: Any) -> AsyncRedisFake:
            assert kwargs["max_connections"] == 100
            return built_async

    redis_module = types.ModuleType("redis")
    redis_module.Redis = RedisFactory  # type: ignore[attr-defined]
    redis_async = types.ModuleType("redis.asyncio")
    redis_async.Redis = AsyncRedisFactory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_async)
    lazy = RedisConnectionLifecycle(RedisConnectionConfig(url="redis://lazy/0"))
    assert lazy.sync() is built_sync
    assert lazy.async_client() is built_async


@pytest.mark.asyncio
async def test_async_connection_lifecycle_and_borrowed_clients() -> None:
    connections, sync, async_client = lifecycle(owns=True)
    await connections.aclose()
    await connections.aclose()
    assert sync.closed == async_client.closed == 1

    borrowed, sync, async_client = lifecycle(owns=False)
    await borrowed.aclose()
    assert sync.closed == async_client.closed == 0


def test_pydantic_codec_and_redis_cache() -> None:
    codec = PydanticResponseCodec({"payload": Payload})
    encoded = codec.encode(Payload(value="ok"))
    assert codec.decode(encoded) == Payload(value="ok")
    assert codec.decode(encoded.encode()) == Payload(value="ok")
    with pytest.raises(TypeError, match="unsupported"):
        PydanticResponseCodec({}).encode(Payload(value="x"))
    with pytest.raises(ValueError, match="schema"):
        codec.decode('{"schema":2}')
    with pytest.raises(ValueError, match="unregistered"):
        codec.decode('{"schema":1,"type":"other","value":{}}')

    connections, sync, _ = lifecycle()
    cache = RedisModelCache(connections, key_prefix="harbor:", codec=codec)
    assert cache.get("missing") is None
    cache.set("a", Payload(value="cached"), 30)
    assert "harbor:cache:a" in sync.values
    assert cache.get("a") == Payload(value="cached")
    cache.close()


@pytest.mark.asyncio
async def test_async_redis_cache_and_owned_close() -> None:
    connections, sync, async_client = lifecycle(owns=True)
    cache = RedisModelCache(
        connections,
        key_prefix="harbor",
        codec=PydanticResponseCodec({"payload": Payload}),
        owns_connections=True,
    )
    assert await cache.aget("missing") is None
    await cache.aset("a", Payload(value="async"), 10)
    assert await cache.aget("a") == Payload(value="async")
    await cache.aclose()
    assert sync.closed == async_client.closed == 1


def test_memory_routing_admission_circuit_and_health() -> None:
    now = [120.0]
    store = InMemoryRoutingStateStore(clock=lambda: now[0])
    lease = store.acquire("primary:a", max_parallel=1, rpm=2, tpm=10, token_cost=4, lease_seconds=5)
    assert store.snapshot("primary:a").active_requests == 1
    with pytest.raises(RoutingAdmissionError, match="concurrency"):
        store.acquire("primary:a", max_parallel=1, rpm=2, tpm=10, token_cost=1, lease_seconds=5)
    store.release(lease)
    store.release(lease)
    second = store.acquire(
        "primary:a", max_parallel=1, rpm=2, tpm=10, token_cost=5, lease_seconds=5
    )
    store.release(second)
    with pytest.raises(RoutingAdmissionError, match="rpm"):
        store.acquire("primary:a", max_parallel=1, rpm=2, tpm=10, token_cost=1, lease_seconds=5)

    now[0] += 60
    with pytest.raises(RoutingAdmissionError, match="tpm"):
        store.acquire("primary:a", max_parallel=1, rpm=2, tpm=3, token_cost=4, lease_seconds=5)
    store.record_failure("primary:a", retryable=False, threshold=1, recovery_seconds=10)
    store.record_failure("primary:a", retryable=True, threshold=1, recovery_seconds=10)
    with pytest.raises(RoutingAdmissionError, match="circuit_open"):
        store.acquire(
            "primary:a",
            max_parallel=1,
            rpm=None,
            tpm=None,
            token_cost=0,
            lease_seconds=5,
        )
    store.record_success("primary:a", 12.5)
    store.record_active_health("primary:a", healthy=False, latency_ms=None)
    snapshot = store.snapshot("primary:a")
    assert snapshot.last_latency_ms == 12.5
    assert not snapshot.available(now[0], health_stale_seconds=30)
    assert snapshot.available(now[0] + 31, health_stale_seconds=30)
    store.close()
    assert store.snapshot("primary:a") == RoutingStateSnapshot()


@pytest.mark.asyncio
async def test_memory_routing_async_operations() -> None:
    store = InMemoryRoutingStateStore(clock=lambda: 1.0)
    lease = await store.aacquire(
        "m:d", max_parallel=2, rpm=None, tpm=None, token_cost=0, lease_seconds=1
    )
    await store.arelease(lease)
    await store.arecord_failure("m:d", retryable=True, threshold=2, recovery_seconds=1)
    await store.arecord_success("m:d", 4.0)
    await store.arecord_active_health("m:d", healthy=True, latency_ms=3.0)
    assert (await store.asnapshot("m:d")).active_healthy is True
    await store.aclose()


def test_redis_routing_state_all_operations() -> None:
    connections, sync, _ = lifecycle()
    store = RedisRoutingStateStore(connections, key_prefix="harbor", clock=lambda: 12.0)
    state_key = "harbor:route:primary:a:state"
    sync.hashes[state_key] = {
        b"active": b"2",
        b"failures": b"3",
        b"open_until": b"20",
        b"latency_ms": b"9.5",
        b"active_healthy": b"0",
        b"active_checked_at": b"10",
    }
    snapshot = store.snapshot("primary:a")
    assert snapshot.active_requests == 2
    assert snapshot.active_healthy is False

    sync.eval_results.extend([[1, b"ok"], [0, b"rpm"]])
    lease = store.acquire(
        "primary:a", max_parallel=2, rpm=5, tpm=100, token_cost=7, lease_seconds=30
    )
    assert lease.deployment_key == "primary:a"
    with pytest.raises(RoutingAdmissionError) as error:
        store.acquire(
            "primary:a",
            max_parallel=None,
            rpm=None,
            tpm=None,
            token_cost=0,
            lease_seconds=30,
        )
    assert error.value.reason == "rpm"
    store.release(lease)
    store.record_success("primary:a", 5.0)
    store.record_failure("primary:a", retryable=False, threshold=2, recovery_seconds=10)
    calls = len(sync.eval_calls)
    store.record_failure("primary:a", retryable=True, threshold=2, recovery_seconds=10)
    assert len(sync.eval_calls) == calls + 1
    store.record_active_health("primary:a", healthy=True, latency_ms=None)
    store.close()


@pytest.mark.asyncio
async def test_redis_routing_state_async_operations_and_owned_close() -> None:
    connections, sync, async_client = lifecycle(owns=True)
    store = RedisRoutingStateStore(
        connections, key_prefix="harbor", owns_connections=True, clock=lambda: 20.0
    )
    sync.eval_results.append([1, "ok"])
    lease = await store.aacquire(
        "m:d", max_parallel=None, rpm=None, tpm=None, token_cost=0, lease_seconds=10
    )
    await store.arelease(lease)
    await store.arecord_success("m:d", 2.0)
    await store.arecord_failure("m:d", retryable=True, threshold=1, recovery_seconds=5)
    await store.arecord_failure("m:d", retryable=False, threshold=1, recovery_seconds=5)
    await store.arecord_active_health("m:d", healthy=False, latency_ms=1.0)
    await store.asnapshot("m:d")
    await store.aclose()
    assert sync.closed == async_client.closed == 1


def test_routing_snapshot_availability_and_admission_error() -> None:
    assert RoutingStateSnapshot().available(10, health_stale_seconds=5)
    assert not RoutingStateSnapshot(circuit_open_until=11).available(10, health_stale_seconds=5)
    error = RoutingAdmissionError("tpm")
    assert error.reason == "tpm"
    assert "tpm" in str(error)
    assert RoutingLease("x", "y").lease_id == "y"
