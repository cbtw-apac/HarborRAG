from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import BaseModel

from harborrag_adapters.models.runtime.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.runtime.redis_config import RedisConnectionConfig
from harborrag_adapters.models.runtime.singleflight import (
    _RELEASE_LOCK_SCRIPT,
    _RENEW_LOCK_SCRIPT,
    RedisSingleFlight,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

LEADER_SECONDS = 1.6


class Response(BaseModel):
    value: str


class ExpiringRedis:
    """Model SET NX with expiry and the lock scripts against a monotonic clock."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self.renewals = 0

    def _live(self, name: str) -> Any:
        entry = self._values.get(name)
        if entry is None or entry[1] <= time.monotonic():
            self._values.pop(name, None)
            return None
        return entry[0]

    def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        with self._lock:
            if kwargs.get("nx") and self._live(name) is not None:
                return False
            self._values[name] = (value, time.monotonic() + kwargs["ex"])
            return True

    def get(self, name: str) -> Any:
        with self._lock:
            return self._live(name)

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        del numkeys
        with self._lock:
            name, token = args[0], args[1]
            if self._live(name) != token:
                return 0
            if script == _RELEASE_LOCK_SCRIPT:
                del self._values[name]
                return 1
            assert script == _RENEW_LOCK_SCRIPT
            self.renewals += 1
            self._values[name] = (token, time.monotonic() + int(args[2]) / 1_000)
            return 1

    def delete(self, *names: str) -> int:
        with self._lock:
            return sum(self._values.pop(name, None) is not None for name in names)

    def hgetall(self, name: str) -> dict[str, str]:
        del name
        return {}

    def close(self) -> None:
        return None


class ExpiringRedisAsync:
    def __init__(self, sync: ExpiringRedis) -> None:
        self.sync = sync

    async def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        return self.sync.set(name, value, **kwargs)

    async def get(self, name: str) -> Any:
        return self.sync.get(name)

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        return self.sync.eval(script, numkeys, *args)

    async def delete(self, *names: str) -> int:
        return self.sync.delete(*names)

    async def hgetall(self, name: str) -> dict[str, str]:
        return self.sync.hgetall(name)

    async def aclose(self) -> None:
        return None


def coordinator_for(redis: ExpiringRedis) -> RedisSingleFlight:
    connections = RedisConnectionLifecycle(
        RedisConnectionConfig(url="redis://localhost"),
        sync_client=redis,
        async_client=ExpiringRedisAsync(redis),
        owns_clients=False,
    )
    return RedisSingleFlight(
        connections,
        key_prefix="lease",
        lock_ttl_seconds=1,
        follower_timeout_seconds=4,
        poll_interval_seconds=0.01,
    )


def test_sync_leader_renews_its_lease_while_running_past_the_ttl() -> None:
    redis = ExpiringRedis()
    coordinator = coordinator_for(redis)
    cache: dict[str, Response] = {}
    started = threading.Event()
    calls = 0

    def producer() -> Response:
        nonlocal calls
        calls += 1
        started.set()
        time.sleep(LEADER_SECONDS)
        cache["k"] = Response(value="leader")
        return cache["k"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(coordinator.execute, "k", producer, lambda: cache.get("k"))
        started.wait(2)
        follower = pool.submit(coordinator.execute, "k", producer, lambda: cache.get("k"))
        leader_result = leader.result(5)
        follower_result = follower.result(5)

    assert calls == 1
    assert leader_result.shared is False
    assert follower_result.shared is True
    assert redis.renewals >= 1
    renewals = redis.renewals
    time.sleep(0.5)
    assert redis.renewals == renewals
    assert redis.get("lease:singleflight:k") is None
    assert not [thread for thread in threading.enumerate() if "singleflight" in thread.name]


@pytest.mark.asyncio
async def test_async_leader_renews_its_lease_while_running_past_the_ttl() -> None:
    redis = ExpiringRedis()
    coordinator = coordinator_for(redis)
    cache: dict[str, Response] = {}
    started = asyncio.Event()
    calls = 0

    async def producer() -> Response:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(LEADER_SECONDS)
        cache["k"] = Response(value="leader")
        return cache["k"]

    async def loader() -> Response | None:
        return cache.get("k")

    leader = asyncio.create_task(coordinator.aexecute("k", producer, loader))
    await started.wait()
    follower = asyncio.create_task(coordinator.aexecute("k", producer, loader))
    leader_result, follower_result = await asyncio.wait_for(asyncio.gather(leader, follower), 5)

    assert calls == 1
    assert leader_result.shared is False
    assert follower_result.shared is True
    assert redis.renewals >= 1
    renewals = redis.renewals
    await asyncio.sleep(0.5)
    assert redis.renewals == renewals
    assert redis.get("lease:singleflight:k") is None


@pytest.mark.asyncio
async def test_async_renewal_stops_when_the_leader_is_cancelled() -> None:
    redis = ExpiringRedis()
    coordinator = coordinator_for(redis)
    started = asyncio.Event()

    async def producer() -> Response:
        started.set()
        await asyncio.Event().wait()
        return Response(value="never")

    async def loader() -> Response | None:
        return None

    task = asyncio.create_task(coordinator.aexecute("k", producer, loader))
    await started.wait()
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    renewals = redis.renewals
    await asyncio.sleep(0.5)

    assert renewals >= 1
    assert redis.renewals == renewals
    assert redis.get("lease:singleflight:k") is None


def test_renewal_stops_without_raising_when_the_lock_was_lost() -> None:
    redis = ExpiringRedis()
    coordinator = coordinator_for(redis)

    def producer() -> Response:
        redis.delete("lease:singleflight:k")
        time.sleep(0.8)
        return Response(value="leader")

    result = coordinator.execute("k", producer, lambda: None)

    assert result.value.value == "leader"
    assert redis.renewals == 0


def test_follower_takes_over_a_crashed_leader_after_the_ttl() -> None:
    redis = ExpiringRedis()
    coordinator = coordinator_for(redis)
    # A crashed leader holds the lock: no renewer runs and nothing releases it.
    redis.set("lease:singleflight:k", "crashed-leader", nx=True, ex=1)
    started = time.monotonic()

    result = coordinator.execute("k", lambda: Response(value="takeover"), lambda: None)

    assert result.shared is False
    assert result.value.value == "takeover"
    assert time.monotonic() - started >= 0.9
    assert redis.get("lease:singleflight:k") is None
