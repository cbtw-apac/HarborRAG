from __future__ import annotations

import asyncio
from typing import Any

import pytest
from harborrag_adapters.models.common.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.common.redis_config import RedisConnectionConfig
from harborrag_adapters.models.common.routing_state import RoutingAdmissionError
from harborrag_adapters.models.common.routing_state_memory import InMemoryRoutingStateStore
from harborrag_adapters.models.common.singleflight import RedisSingleFlight
from pydantic import BaseModel

pytestmark = pytest.mark.chaos


class Result(BaseModel):
    value: str


class RedisChaosSync:
    def __init__(self) -> None:
        self.releases = 0

    def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        del name, value, kwargs
        return True

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        del script, numkeys, args
        self.releases += 1
        return 1

    def get(self, name: str) -> Any:
        del name
        return None

    def delete(self, *names: str) -> int:
        del names
        return 0

    def hgetall(self, name: str) -> dict[str, str]:
        del name
        return {}

    def close(self) -> None:
        return None


class RedisChaosAsync:
    def __init__(self, sync: RedisChaosSync) -> None:
        self.sync = sync

    async def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        return self.sync.set(name, value, **kwargs)

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        return self.sync.eval(script, numkeys, *args)

    async def get(self, name: str) -> Any:
        return self.sync.get(name)

    async def delete(self, *names: str) -> int:
        return self.sync.delete(*names)

    async def hgetall(self, name: str) -> dict[str, str]:
        return self.sync.hgetall(name)

    async def aclose(self) -> None:
        return None


def test_distributed_circuit_opens_and_recovers_after_clock_advance() -> None:
    now = [10.0]
    store = InMemoryRoutingStateStore(clock=lambda: now[0])
    store.record_failure("primary:a", retryable=True, threshold=2, recovery_seconds=5)
    store.record_failure("primary:a", retryable=True, threshold=2, recovery_seconds=5)
    with pytest.raises(RoutingAdmissionError, match="circuit_open"):
        store.acquire(
            "primary:a",
            max_parallel=None,
            rpm=None,
            tpm=None,
            token_cost=0,
            lease_seconds=1,
        )
    now[0] = 16.0
    lease = store.acquire(
        "primary:a",
        max_parallel=1,
        rpm=None,
        tpm=None,
        token_cost=0,
        lease_seconds=1,
    )
    store.release(lease)


def test_redis_singleflight_releases_lock_when_leader_crashes() -> None:
    sync = RedisChaosSync()
    connections = RedisConnectionLifecycle(
        RedisConnectionConfig(url="redis://localhost"),
        sync_client=sync,
        async_client=RedisChaosAsync(sync),
        owns_clients=False,
    )
    coordinator = RedisSingleFlight(
        connections,
        key_prefix="chaos",
        lock_ttl_seconds=2,
        follower_timeout_seconds=3,
        poll_interval_seconds=0.01,
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        coordinator.execute(
            "request",
            lambda: (_ for _ in ()).throw(RuntimeError("provider failed")),
            lambda: None,
        )
    assert sync.releases == 1


@pytest.mark.asyncio
async def test_async_singleflight_releases_lock_on_cancellation() -> None:
    sync = RedisChaosSync()
    connections = RedisConnectionLifecycle(
        RedisConnectionConfig(url="redis://localhost"),
        sync_client=sync,
        async_client=RedisChaosAsync(sync),
        owns_clients=False,
    )
    coordinator = RedisSingleFlight(
        connections,
        key_prefix="chaos",
        lock_ttl_seconds=2,
        follower_timeout_seconds=3,
        poll_interval_seconds=0.01,
    )
    started = asyncio.Event()

    async def producer() -> Result:
        started.set()
        await asyncio.Event().wait()
        return Result(value="never")

    async def follower() -> Result | None:
        return None

    task = asyncio.create_task(coordinator.aexecute("request", producer, follower))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sync.releases == 1
