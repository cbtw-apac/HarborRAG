"""Per-user, per-principal and per-tenant API rate and concurrency limits."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from harborrag_core.contracts.errors import HarborConnectionError

from .capacity_scope import (
    CapacityBucket,
    CapacityLimits,
    CapacityScope,
    capacity_hash_tag,
    inflight_limit_rejection,
    plan_capacity_buckets,
    rate_limit_rejection,
    resolve_capacity_limits,
)

# Every tier is checked before anything is written, so a rejection returns
# before the first HSET/ZADD: reserving is all-or-nothing without a rollback.
_RESERVE_SCRIPT = """
local clock = redis.call('TIME')
local now_ms = (clock[1] * 1000) + math.floor(clock[2] / 1000)
local window = math.floor(clock[1] / 60)
local buckets = tonumber(ARGV[1])
local counts = {}
for index = 1, buckets do
  local rate_key = KEYS[(index * 2) - 1]
  local inflight_key = KEYS[index * 2]
  local stored_window = tonumber(redis.call('HGET', rate_key, 'window'))
  local count = tonumber(redis.call('HGET', rate_key, 'count')) or 0
  if stored_window ~= window then count = 0 end
  if count >= tonumber(ARGV[index * 2]) then return -((index * 2) - 1) end
  redis.call('ZREMRANGEBYSCORE', inflight_key, '-inf', now_ms)
  if redis.call('ZCARD', inflight_key) >= tonumber(ARGV[(index * 2) + 1]) then
    return -(index * 2)
  end
  counts[index] = count + 1
end
local lease_ms = tonumber(ARGV[(buckets * 2) + 2])
local expiry_ms = now_ms + lease_ms
for index = 1, buckets do
  redis.call('HSET', KEYS[(index * 2) - 1], 'window', window, 'count', counts[index])
  redis.call('PEXPIRE', KEYS[(index * 2) - 1], 61000)
  redis.call('ZADD', KEYS[index * 2], expiry_ms, ARGV[(buckets * 2) + 3])
  redis.call('PEXPIRE', KEYS[index * 2], lease_ms + 1000)
end
return counts[buckets]
"""

_RELEASE_SCRIPT = """
for index = 1, #KEYS do
  redis.call('ZREM', KEYS[index], ARGV[1])
end
return 1
"""


class ApiCapacityLimiter(Protocol):
    """Reserve and release one expensive API execution slot."""

    async def reserve(self, scope: CapacityScope) -> str: ...

    async def release(self, scope: CapacityScope, lease_id: str) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class LocalApiCapacityLimiter:
    """Single-process capacity guard for explicit development mode."""

    limits: CapacityLimits
    lease_seconds: float = 300.0
    tenant_overrides: Mapping[str, CapacityLimits] = field(default_factory=dict)
    _lock: asyncio.Lock = field(init=False, repr=False)
    _windows: dict[str, tuple[int, int]] = field(init=False, repr=False)
    _leases: dict[str, dict[str, float]] = field(init=False, repr=False)
    _last_pruned_window: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_lease_seconds(self.lease_seconds)
        self._lock = asyncio.Lock()
        self._windows = {}
        self._leases = {}
        self._last_pruned_window = _current_window()

    async def reserve(self, scope: CapacityScope) -> str:
        buckets = self._buckets(scope)
        now = time.monotonic()
        current_window = _current_window(now)
        async with self._lock:
            self._prune(current_window, now)
            # Admit against every tier first: raising before the commit loop
            # keeps a rejected reservation from leaking a count or a lease
            # into the tiers that did have room.
            counts = [self._admit(bucket, current_window) for bucket in buckets]
            lease_id = uuid4().hex
            for bucket, count in zip(buckets, counts, strict=True):
                self._windows[bucket.identity] = (current_window, count)
                self._leases.setdefault(bucket.identity, {})[lease_id] = now + self.lease_seconds
            return lease_id

    async def release(self, scope: CapacityScope, lease_id: str) -> None:
        async with self._lock:
            for bucket in self._buckets(scope):
                leases = self._leases.get(bucket.identity)
                if leases is None:
                    continue
                leases.pop(lease_id, None)
                if not leases:
                    self._leases.pop(bucket.identity, None)

    async def aclose(self) -> None:
        """No resources are owned by the local implementation."""

    def _buckets(self, scope: CapacityScope) -> tuple[CapacityBucket, ...]:
        limits = resolve_capacity_limits(scope.tenant_id, self.limits, self.tenant_overrides)
        return plan_capacity_buckets(scope, limits)

    def _admit(self, bucket: CapacityBucket, current_window: int) -> int:
        window, count = self._windows.get(bucket.identity, (current_window, 0))
        if window != current_window:
            count = 0
        if count >= bucket.requests_per_minute:
            raise rate_limit_rejection(bucket.rate_tier)
        if len(self._leases.get(bucket.identity, {})) >= bucket.max_inflight:
            raise inflight_limit_rejection(bucket.inflight_tier)
        return count + 1

    def _prune(self, current_window: int, now: float) -> None:
        if current_window != self._last_pruned_window:
            self._windows = {
                key: value for key, value in self._windows.items() if value[0] == current_window
            }
            self._last_pruned_window = current_window
        for key, leases in tuple(self._leases.items()):
            active = {lease_id: expiry for lease_id, expiry in leases.items() if expiry > now}
            if active:
                self._leases[key] = active
            else:
                self._leases.pop(key, None)


@dataclass(slots=True)
class RedisApiCapacityLimiter:
    """Cross-replica fixed-window rate and leased concurrent-request guard."""

    client: Any
    limits: CapacityLimits
    lease_seconds: float
    tenant_overrides: Mapping[str, CapacityLimits] = field(default_factory=dict)
    key_prefix: str = "harborrag-api-capacity"

    def __post_init__(self) -> None:
        _validate_lease_seconds(self.lease_seconds)
        if not self.key_prefix or any(
            character.isspace() or character in "{}" for character in self.key_prefix
        ):
            raise ValueError(
                "API capacity Redis key_prefix must be non-empty without whitespace or braces"
            )

    async def reserve(self, scope: CapacityScope) -> str:
        buckets = self._buckets(scope)
        keys = self._keys(scope, buckets)
        arguments: list[object] = [len(buckets)]
        for bucket in buckets:
            arguments.extend((bucket.requests_per_minute, bucket.max_inflight))
        lease_id = uuid4().hex
        arguments.extend((math.ceil(self.lease_seconds * 1000), lease_id))
        result = await self._eval(_RESERVE_SCRIPT, keys, arguments)
        _raise_for_reserve_result(result, buckets)
        return lease_id

    async def release(self, scope: CapacityScope, lease_id: str) -> None:
        buckets = self._buckets(scope)
        inflight_keys = self._keys(scope, buckets)[1::2]
        await self._eval(_RELEASE_SCRIPT, inflight_keys, [lease_id])

    async def aclose(self) -> None:
        close: Callable[[], Awaitable[None]] | None = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    def _buckets(self, scope: CapacityScope) -> tuple[CapacityBucket, ...]:
        limits = resolve_capacity_limits(scope.tenant_id, self.limits, self.tenant_overrides)
        return plan_capacity_buckets(scope, limits)

    def _keys(self, scope: CapacityScope, buckets: tuple[CapacityBucket, ...]) -> list[str]:
        # The tenant-derived hash tag keeps every tier's keys in one Redis
        # Cluster slot, which is what makes the multi-key script atomic.
        hash_tag = f"{{{capacity_hash_tag(scope)}}}"
        keys: list[str] = []
        for bucket in buckets:
            stem = f"{self.key_prefix}:{hash_tag}:{bucket.identity}"
            keys.extend((f"{stem}:rate", f"{stem}:inflight"))
        return keys

    async def _eval(self, script: str, keys: list[str], arguments: list[object]) -> object:
        try:
            return await self.client.eval(script, len(keys), *keys, *arguments)
        except Exception as exc:
            raise HarborConnectionError("API capacity service is unavailable") from exc


def build_api_capacity_limiter(
    *,
    redis_url: str | None,
    limits: CapacityLimits,
    lease_seconds: float,
    tenant_overrides: Mapping[str, CapacityLimits] | None = None,
) -> ApiCapacityLimiter:
    """Use Redis when configured; reserve local state for development."""
    overrides: Mapping[str, CapacityLimits] = dict(tenant_overrides or {})
    if redis_url is None:
        return LocalApiCapacityLimiter(limits, lease_seconds, overrides)
    from redis.asyncio import Redis

    return RedisApiCapacityLimiter(
        Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2),
        limits,
        lease_seconds,
        overrides,
    )


def _raise_for_reserve_result(result: object, buckets: tuple[CapacityBucket, ...]) -> None:
    """Map the script's return value onto a rejection or an invalid response.

    Rejections encode the offending bucket: ``-(2i-1)`` is bucket ``i``'s rate
    limit and ``-2i`` its concurrency limit, so a merged user/principal plan of
    two buckets can never legitimately answer -5 or -6.
    """
    if isinstance(result, bool) or not isinstance(result, int):
        raise HarborConnectionError("API capacity service returned an invalid response")
    if result < 0:
        index = (abs(result) + 1) // 2
        if index > len(buckets):
            raise HarborConnectionError("API capacity service returned an invalid response")
        bucket = buckets[index - 1]
        if abs(result) % 2 == 1:
            raise rate_limit_rejection(bucket.rate_tier)
        raise inflight_limit_rejection(bucket.inflight_tier)
    if result < 1:
        raise HarborConnectionError("API capacity service returned an invalid response")


def _current_window(now: float | None = None) -> int:
    return int((time.monotonic() if now is None else now) // 60)


def _validate_lease_seconds(lease_seconds: float) -> None:
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, (int, float))
        or not math.isfinite(lease_seconds)
        or lease_seconds <= 0
    ):
        raise ValueError("API lease_seconds must be a finite positive number")


__all__ = [
    "ApiCapacityLimiter",
    "LocalApiCapacityLimiter",
    "RedisApiCapacityLimiter",
    "build_api_capacity_limiter",
]
