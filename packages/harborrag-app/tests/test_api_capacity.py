"""Rate and concurrent-execution guards for expensive API requests."""

from __future__ import annotations

import math

import pytest

from harborrag_app.api.capacity import (
    LocalApiCapacityLimiter,
    RedisApiCapacityLimiter,
    build_api_capacity_limiter,
)
from harborrag_core.contracts.errors import HarborConnectionError, HarborRateLimitError


@pytest.mark.asyncio
async def test_local_capacity_enforces_inflight_and_rate_limits() -> None:
    limiter = LocalApiCapacityLimiter(requests_per_minute=2, max_inflight=1)

    first = await limiter.reserve("principal")
    with pytest.raises(HarborRateLimitError, match="concurrent"):
        await limiter.reserve("principal")
    await limiter.release("principal", first)

    second = await limiter.reserve("principal")
    await limiter.release("principal", second)
    with pytest.raises(HarborRateLimitError, match="rate"):
        await limiter.reserve("principal")


@pytest.mark.asyncio
async def test_local_capacity_expires_abandoned_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harborrag_app.api.capacity as capacity_module

    now = 100.0
    monkeypatch.setattr(capacity_module.time, "monotonic", lambda: now)
    limiter = LocalApiCapacityLimiter(
        requests_per_minute=2,
        max_inflight=1,
        lease_seconds=5,
    )
    await limiter.reserve("principal")

    now = 106.0
    replacement = await limiter.reserve("principal")

    await limiter.release("principal", replacement)


def test_capacity_limiters_reject_invalid_limits() -> None:
    for invalid in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError):
            LocalApiCapacityLimiter(10, 2, invalid)
        with pytest.raises(ValueError):
            RedisApiCapacityLimiter(_FakeRedis(1), 10, 2, invalid)

    with pytest.raises(ValueError):
        LocalApiCapacityLimiter(0, 2)
    with pytest.raises(ValueError):
        RedisApiCapacityLimiter(_FakeRedis(1), 10, 0, 30)
    with pytest.raises(ValueError, match="key_prefix"):
        RedisApiCapacityLimiter(_FakeRedis(1), 10, 2, 30, "unsafe{prefix}")


class _FakeRedis:
    def __init__(self, result: int | Exception) -> None:
        self.result = result
        self.removed: tuple[str, str] | None = None
        self.eval_arguments: tuple[object, ...] | None = None

    async def eval(self, *args: object) -> int:
        self.eval_arguments = args
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def zrem(self, key: str, lease_id: str) -> None:
        self.removed = (key, lease_id)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "message"),
    [(-1, "rate"), (-2, "concurrent")],
)
async def test_redis_capacity_maps_atomic_reservation_rejections(
    result: int,
    message: str,
) -> None:
    limiter = RedisApiCapacityLimiter(_FakeRedis(result), 10, 2, 30)

    with pytest.raises(HarborRateLimitError, match=message):
        await limiter.reserve("principal")


@pytest.mark.asyncio
async def test_redis_capacity_fails_closed_when_backend_is_unavailable() -> None:
    limiter = RedisApiCapacityLimiter(_FakeRedis(ConnectionError("offline")), 10, 2, 30)

    with pytest.raises(HarborConnectionError, match="capacity service"):
        await limiter.reserve("principal")


@pytest.mark.asyncio
async def test_redis_capacity_releases_hashed_principal_lease() -> None:
    client = _FakeRedis(1)
    limiter = RedisApiCapacityLimiter(client, 10, 2, 30)
    lease_id = await limiter.reserve("sensitive-principal")

    await limiter.release("sensitive-principal", lease_id)

    assert client.removed is not None
    key, removed_lease = client.removed
    assert "sensitive-principal" not in key
    assert client.eval_arguments is not None
    assert key == client.eval_arguments[3]
    assert removed_lease == lease_id


@pytest.mark.asyncio
async def test_redis_capacity_uses_server_time_and_cluster_safe_keys() -> None:
    client = _FakeRedis(1)
    limiter = RedisApiCapacityLimiter(client, 10, 2, 30)

    await limiter.reserve("principal")

    assert client.eval_arguments is not None
    script, key_count, rate_key, inflight_key, *arguments = client.eval_arguments
    assert key_count == 2
    assert isinstance(script, str) and "redis.call('TIME')" in script
    assert isinstance(rate_key, str)
    assert isinstance(inflight_key, str)
    hash_tag = rate_key[rate_key.index("{") : rate_key.index("}") + 1]
    assert len(hash_tag) == 66
    assert f":{hash_tag}:" in inflight_key
    assert "principal" not in rate_key
    assert arguments[:3] == [10, 2, 30_000]


@pytest.mark.asyncio
async def test_redis_capacity_rejects_unexpected_script_results() -> None:
    limiter = RedisApiCapacityLimiter(_FakeRedis(0), 10, 2, 30)

    with pytest.raises(HarborConnectionError, match="invalid response"):
        await limiter.reserve("principal")


def test_capacity_builder_applies_lease_duration_to_local_limiter() -> None:
    limiter = build_api_capacity_limiter(
        redis_url=None,
        requests_per_minute=10,
        max_inflight=2,
        lease_seconds=15,
    )

    assert isinstance(limiter, LocalApiCapacityLimiter)
    assert limiter.lease_seconds == 15
