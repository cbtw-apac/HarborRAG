"""Client-side request budgets.

One :class:`TokenBucket` is shared by every request of one connection, so a
connector's concurrent sub-fetches cannot outrun the source's limits. A
distributed limiter (Redis sliding window) can replace it later behind the
same :class:`RateLimiter` protocol; :class:`RedisTokenBucket` is that
limiter, reached through a small protocol so ``redis`` stays optional.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger("harborrag.adapters.sources.kit.rate_limit")

_SAFE_KEY_PART = re.compile(r"[^a-z0-9._-]+")

# A bucket of ARGV[1] tokens refilling ARGV[2] per millisecond; ARGV[3] is the
# key's TTL in ms and ARGV[4] the cost. Returns {allowed, wait_ms}.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local clock = redis.call("TIME")
local now_ms = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
local state = redis.call("HMGET", key, "tokens", "updated_ms")
local tokens
local updated_ms

if state[1] == false then
    tokens = math.min(capacity, cost)
    updated_ms = now_ms
else
    tokens = tonumber(state[1])
    updated_ms = tonumber(state[2])
    tokens = math.min(capacity, tokens + math.max(0, now_ms - updated_ms) * refill_per_ms)
end

local allowed = 0
local wait_ms = 0
if tokens >= cost then
    allowed = 1
    tokens = tokens - cost
else
    wait_ms = math.ceil((cost - tokens) / refill_per_ms)
end

redis.call("HSET", key, "tokens", tostring(tokens), "updated_ms", tostring(now_ms))
redis.call("PEXPIRE", key, ttl_ms)
return {allowed, wait_ms}
"""


class RateLimiter(Protocol):
    async def acquire(self, cost: int = 1) -> None:
        """Wait until ``cost`` request units may be spent."""
        ...


class UnlimitedRateLimiter:
    """No client-side limit; the source's own throttling still applies."""

    async def acquire(self, cost: int = 1) -> None:
        del cost


class TokenBucket:
    """Classic token bucket: ``rate_per_second`` refill, ``burst`` capacity."""

    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = clock()
        self._lock = asyncio.Lock()

    @classmethod
    def per_hour(cls, requests: int, *, burst: int | None = None) -> TokenBucket:
        """A bucket for an hourly budget, bursting up to a minute's share by default."""

        if requests < 1:
            raise ValueError("hourly budget must be positive")
        return cls(requests / 3600.0, burst or max(1, requests // 60))

    async def acquire(self, cost: int = 1) -> None:
        if cost < 1 or cost > self._burst:
            raise ValueError(f"cost must be between 1 and the burst size {self._burst:g}")
        async with self._lock:
            self._refill()
            if self._tokens < cost:
                await self._sleep((cost - self._tokens) / self._rate)
                self._refill()
            self._tokens -= cost

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._updated = now


class RedisScriptClient(Protocol):
    """The slice of an async Redis client the bucket needs (``redis.asyncio``)."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...


def _safe_key_part(value: str) -> str:
    return _SAFE_KEY_PART.sub("-", value.strip().lower()).strip("-") or "unknown"


def rate_limit_key(prefix: str, connection_id: str, host: str) -> str:
    """A readable Redis key from non-secret parts: one bucket per connection and host."""

    return ":".join(_safe_key_part(part) for part in (prefix, connection_id, host))


def _parse_lua_result(result: object) -> tuple[bool, int]:
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise ValueError("Redis token bucket returned an invalid response")
    return bool(int(result[0])), max(0, int(result[1]))


class RedisTokenBucket:
    """A token bucket shared across processes through one Lua script.

    ``burst`` is the capacity and ``rate_per_second`` the refill. When Redis
    fails, requests go through ``fallback`` (warning once per outage) and the
    next call tries Redis again.
    """

    def __init__(  # noqa: PLR0913 - keyword-only limiter settings
        self,
        client: RedisScriptClient,
        *,
        key: str,
        rate_per_second: float,
        burst: int,
        fallback: RateLimiter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ttl_seconds: float = 120.0,
    ) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self._client = client
        self._key = key
        self._burst = burst
        self._refill_per_ms = rate_per_second / 1000.0
        self._ttl_ms = max(1, round(ttl_seconds * 1000))
        self._fallback = fallback
        self._sleep = sleep
        self._redis_available = True

    async def acquire(self, cost: int = 1) -> None:
        if cost < 1 or cost > self._burst:
            raise ValueError(f"cost must be between 1 and the burst size {self._burst}")
        while True:
            try:
                allowed, wait_ms = await self._try_once(cost)
            except Exception as error:
                if self._redis_available:
                    logger.warning(
                        "Redis rate limiter unavailable; using local fallback (%s)",
                        type(error).__name__,
                    )
                self._redis_available = False
                await self._fallback.acquire(cost)
                return
            self._redis_available = True
            if allowed:
                return
            await self._sleep(max(wait_ms, 1) / 1000)

    async def _try_once(self, cost: int) -> tuple[bool, int]:
        result = await self._client.eval(
            _TOKEN_BUCKET_LUA,
            1,
            self._key,
            str(self._burst),
            repr(self._refill_per_ms),
            str(self._ttl_ms),
            str(cost),
        )
        return _parse_lua_result(result)
