"""Per-principal API rate and concurrent-execution limits."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from harborrag_core.contracts.errors import HarborConnectionError, HarborRateLimitError

_RESERVE_SCRIPT = """
local clock = redis.call('TIME')
local now_ms = (clock[1] * 1000) + math.floor(clock[2] / 1000)
local window = math.floor(clock[1] / 60)
local stored_window = tonumber(redis.call('HGET', KEYS[1], 'window'))
local count = tonumber(redis.call('HGET', KEYS[1], 'count')) or 0
if stored_window ~= window then count = 0 end
if count >= tonumber(ARGV[1]) then return -1 end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now_ms)
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return -2 end
count = count + 1
redis.call('HSET', KEYS[1], 'window', window, 'count', count)
redis.call('PEXPIRE', KEYS[1], 61000)
local expiry_ms = now_ms + tonumber(ARGV[3])
redis.call('ZADD', KEYS[2], expiry_ms, ARGV[4])
redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[3]) + 1000)
return count
"""


class ApiCapacityLimiter(Protocol):
    """Reserve and release one expensive API execution slot."""

    async def reserve(self, principal_id: str) -> str: ...

    async def release(self, principal_id: str, lease_id: str) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class LocalApiCapacityLimiter:
    """Single-process capacity guard for explicit development mode."""

    requests_per_minute: int
    max_inflight: int
    lease_seconds: float = 300.0
    _lock: asyncio.Lock = field(init=False, repr=False)
    _windows: dict[str, tuple[int, int]] = field(init=False, repr=False)
    _leases: dict[str, dict[str, float]] = field(init=False, repr=False)
    _last_pruned_window: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_limits(self.requests_per_minute, self.max_inflight, self.lease_seconds)
        self._lock = asyncio.Lock()
        self._windows = {}
        self._leases = {}
        self._last_pruned_window = _current_window()

    async def reserve(self, principal_id: str) -> str:
        key = _principal_key(principal_id)
        now = time.monotonic()
        current_window = _current_window(now)
        async with self._lock:
            self._prune(current_window, now)
            window, count = self._windows.get(key, (current_window, 0))
            if window != current_window:
                window, count = current_window, 0
            if count >= self.requests_per_minute:
                raise HarborRateLimitError("API request rate limit exceeded")
            leases = self._leases.setdefault(key, {})
            if len(leases) >= self.max_inflight:
                raise HarborRateLimitError(
                    "API concurrent request limit exceeded",
                    retry_after_seconds=1,
                )
            lease_id = uuid4().hex
            leases[lease_id] = now + self.lease_seconds
            self._windows[key] = (window, count + 1)
            return lease_id

    async def release(self, principal_id: str, lease_id: str) -> None:
        key = _principal_key(principal_id)
        async with self._lock:
            leases = self._leases.get(key)
            if leases is None:
                return
            leases.pop(lease_id, None)
            if not leases:
                self._leases.pop(key, None)

    async def aclose(self) -> None:
        """No resources are owned by the local implementation."""

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
    requests_per_minute: int
    max_inflight: int
    lease_seconds: float
    key_prefix: str = "harborrag-api-capacity"

    def __post_init__(self) -> None:
        _validate_limits(self.requests_per_minute, self.max_inflight, self.lease_seconds)
        if not self.key_prefix or any(
            character.isspace() or character in "{}" for character in self.key_prefix
        ):
            raise ValueError(
                "API capacity Redis key_prefix must be non-empty without whitespace or braces"
            )

    async def reserve(self, principal_id: str) -> str:
        identity = _principal_key(principal_id)
        # The shared hash tag keeps both keys in one Redis Cluster slot, which is
        # required for an atomic multi-key script.
        rate_key, inflight_key = _redis_capacity_keys(self.key_prefix, identity)
        lease_id = uuid4().hex
        lease_ms = math.ceil(self.lease_seconds * 1000)
        try:
            result = await self.client.eval(
                _RESERVE_SCRIPT,
                2,
                rate_key,
                inflight_key,
                self.requests_per_minute,
                self.max_inflight,
                lease_ms,
                lease_id,
            )
        except Exception as exc:
            raise HarborConnectionError("API capacity service is unavailable") from exc
        if result == -1:
            raise HarborRateLimitError("API request rate limit exceeded")
        if result == -2:
            raise HarborRateLimitError(
                "API concurrent request limit exceeded",
                retry_after_seconds=1,
            )
        if isinstance(result, bool) or not isinstance(result, int) or result < 1:
            raise HarborConnectionError("API capacity service returned an invalid response")
        return lease_id

    async def release(self, principal_id: str, lease_id: str) -> None:
        _, key = _redis_capacity_keys(self.key_prefix, _principal_key(principal_id))
        try:
            await self.client.zrem(key, lease_id)
        except Exception as exc:
            raise HarborConnectionError("API capacity service is unavailable") from exc

    async def aclose(self) -> None:
        close: Callable[[], Awaitable[None]] | None = getattr(self.client, "aclose", None)
        if close is not None:
            await close()


def build_api_capacity_limiter(
    *,
    redis_url: str | None,
    requests_per_minute: int,
    max_inflight: int,
    lease_seconds: float,
) -> ApiCapacityLimiter:
    """Use Redis when configured; reserve local state for development."""
    if redis_url is None:
        return LocalApiCapacityLimiter(requests_per_minute, max_inflight, lease_seconds)
    from redis.asyncio import Redis

    return RedisApiCapacityLimiter(
        Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2),
        requests_per_minute,
        max_inflight,
        lease_seconds,
    )


def _principal_key(principal_id: str) -> str:
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()


def _redis_capacity_keys(key_prefix: str, identity: str) -> tuple[str, str]:
    hash_tag = f"{{{identity}}}"
    return f"{key_prefix}:{hash_tag}:rate", f"{key_prefix}:{hash_tag}:inflight"


def _current_window(now: float | None = None) -> int:
    return int((time.monotonic() if now is None else now) // 60)


def _validate_limits(
    requests_per_minute: int,
    max_inflight: int,
    lease_seconds: float,
) -> None:
    if (
        isinstance(requests_per_minute, bool)
        or not isinstance(requests_per_minute, int)
        or requests_per_minute < 1
    ):
        raise ValueError("API requests_per_minute must be a positive integer")
    if isinstance(max_inflight, bool) or not isinstance(max_inflight, int) or max_inflight < 1:
        raise ValueError("API max_inflight must be a positive integer")
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
