"""Rate and concurrent-execution guards for expensive API requests."""

from __future__ import annotations

import math

import pytest

from harborrag_app.api.capacity import (
    LocalApiCapacityLimiter,
    RedisApiCapacityLimiter,
    build_api_capacity_limiter,
)
from harborrag_app.api.capacity_scope import (
    CapacityLimits,
    CapacityScope,
    CapacityTierLimits,
)
from harborrag_core.contracts.errors import HarborConnectionError, HarborRateLimitError


def scope(
    *,
    user: str = "user-1",
    principal: str = "principal-1",
    tenant: str = "tenant-1",
) -> CapacityScope:
    """Build the three identities one request is charged against."""
    return CapacityScope(tenant_id=tenant, principal_id=principal, user_id=user)


def limits(
    *,
    user: tuple[int, int] = (60, 4),
    principal: tuple[int, int] = (60, 4),
    tenant: tuple[int, int] = (600, 40),
) -> CapacityLimits:
    """Build three-tier limits as ``(requests_per_minute, max_inflight)`` pairs."""
    return CapacityLimits(
        user=CapacityTierLimits(*user),
        principal=CapacityTierLimits(*principal),
        tenant=CapacityTierLimits(*tenant),
    )


class FakeRedis:
    """Record every ``eval`` and answer reservations with a canned result."""

    def __init__(self, result: int | Exception) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> int:
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        return None

    @property
    def reserve_call(self) -> tuple[object, ...]:
        return self.calls[0]


@pytest.mark.asyncio
async def test_local_capacity_enforces_inflight_and_rate_limits() -> None:
    limiter = LocalApiCapacityLimiter(limits(user=(2, 1), principal=(2, 1)))

    first = await limiter.reserve(scope())
    with pytest.raises(HarborRateLimitError, match="concurrent"):
        await limiter.reserve(scope())
    await limiter.release(scope(), first)

    second = await limiter.reserve(scope())
    await limiter.release(scope(), second)
    with pytest.raises(HarborRateLimitError, match="rate"):
        await limiter.reserve(scope())


@pytest.mark.asyncio
async def test_local_capacity_expires_abandoned_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harborrag_app.api.capacity as capacity_module

    now = 100.0
    monkeypatch.setattr(capacity_module.time, "monotonic", lambda: now)
    limiter = LocalApiCapacityLimiter(limits(user=(2, 1), principal=(2, 1)), lease_seconds=5)
    await limiter.reserve(scope())

    now = 106.0
    replacement = await limiter.reserve(scope())

    await limiter.release(scope(), replacement)


def test_capacity_limiters_reject_invalid_limits() -> None:
    for invalid in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError, match="lease_seconds"):
            LocalApiCapacityLimiter(limits(), invalid)
        with pytest.raises(ValueError, match="lease_seconds"):
            RedisApiCapacityLimiter(FakeRedis(1), limits(), invalid)

    with pytest.raises(ValueError, match="requests_per_minute"):
        CapacityTierLimits(0, 2)
    with pytest.raises(ValueError, match="max_inflight"):
        CapacityTierLimits(10, 0)
    with pytest.raises(ValueError, match="key_prefix"):
        RedisApiCapacityLimiter(FakeRedis(1), limits(), 30, {}, "unsafe{prefix}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "message", "tier"),
    [
        (-1, "rate", "user"),
        (-2, "concurrent", "user"),
        (-3, "rate", "principal"),
        (-4, "concurrent", "principal"),
        (-5, "rate", "tenant"),
        (-6, "concurrent", "tenant"),
    ],
)
async def test_redis_capacity_maps_atomic_reservation_rejections(
    result: int,
    message: str,
    tier: str,
) -> None:
    limiter = RedisApiCapacityLimiter(FakeRedis(result), limits(), 30)

    with pytest.raises(HarborRateLimitError, match=message) as rejection:
        await limiter.reserve(scope())

    assert rejection.value.details["limit_scope"] == tier


@pytest.mark.asyncio
async def test_redis_capacity_fails_closed_when_backend_is_unavailable() -> None:
    limiter = RedisApiCapacityLimiter(FakeRedis(ConnectionError("offline")), limits(), 30)

    with pytest.raises(HarborConnectionError, match="capacity service"):
        await limiter.reserve(scope())


@pytest.mark.asyncio
async def test_redis_capacity_releases_every_tier_lease_in_one_script() -> None:
    client = FakeRedis(1)
    limiter = RedisApiCapacityLimiter(client, limits(), 30)
    caller = scope(user="sensitive-user", principal="sensitive-principal")
    lease_id = await limiter.reserve(caller)

    await limiter.release(caller, lease_id)

    script, key_count, *tail = client.calls[1]
    assert "ZREM" in str(script)
    assert key_count == 3
    inflight_keys, arguments = tail[:3], tail[3:]
    assert arguments == [lease_id]
    reserved_keys = client.reserve_call[2:8]
    assert inflight_keys == list(reserved_keys[1::2])
    assert not any("sensitive" in key for key in inflight_keys)


@pytest.mark.asyncio
async def test_redis_capacity_uses_server_time_and_cluster_safe_keys() -> None:
    client = FakeRedis(1)
    limiter = RedisApiCapacityLimiter(client, limits(user=(10, 2), principal=(20, 3)), 30)

    await limiter.reserve(scope())

    script, key_count, *tail = client.reserve_call
    assert key_count == 6
    assert isinstance(script, str) and "redis.call('TIME')" in script
    keys = [key for key in tail[:6] if isinstance(key, str)]
    # One shared hash tag -> one Redis Cluster slot -> an atomic multi-key script.
    hash_tags = {key[key.index("{") : key.index("}") + 1] for key in keys}
    assert len(hash_tags) == 1
    assert len(hash_tags.pop()) == 66
    assert len(set(keys)) == 6
    assert not any("principal-1" in key or "user-1" in key or "tenant-1" in key for key in keys)
    # ARGV: bucket count, then each tier's (rate, concurrency) pair, then the lease.
    assert tail[6:13] == [3, 10, 2, 20, 3, 600, 40]
    assert tail[13] == 30_000


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [0, True, -7])
async def test_redis_capacity_rejects_unexpected_script_results(result: int | bool) -> None:
    limiter = RedisApiCapacityLimiter(FakeRedis(result), limits(), 30)

    with pytest.raises(HarborConnectionError, match="invalid response"):
        await limiter.reserve(scope())


def test_capacity_builder_applies_lease_duration_to_local_limiter() -> None:
    limiter = build_api_capacity_limiter(
        redis_url=None,
        limits=limits(),
        lease_seconds=15,
    )

    assert isinstance(limiter, LocalApiCapacityLimiter)
    assert limiter.lease_seconds == 15
    assert limiter.limits == limits()
