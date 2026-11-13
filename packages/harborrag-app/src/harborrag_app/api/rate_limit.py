"""Per-principal request rate limits.

Handles:
- A sliding one-second window per principal and bucket: ``default``, or ``chat`` for
  completions
- ``429 rate_limited`` with an integer ``Retry-After`` when a call would exceed its bucket
- Windows shared through Redis across replicas, or kept in process for development

Does NOT limit the probes or ``/metrics``, which sit outside ``/v1``, and lets calls through
when Redis is down: losing the limiter must not take the API down with it.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from fastapi import Request

from harborrag_core.contracts.errors import HarborRateLimitError

from .auth.principal import Principal
from .settings import ApiSettings

logger = logging.getLogger("harborrag.app.api.rate_limit")

RateBucket = Literal["default", "chat"]
_WINDOW_SECONDS = 1.0
_LOCAL_KEY_CAP = 10_000
_ACQUIRE_SCRIPT = """
local clock = redis.call('TIME')
local now = (clock[1] * 1000000) + clock[2]
local window = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return math.max(1, tonumber(oldest[2]) + window - now)
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], math.ceil(window / 1000) + 1000)
return 0
"""


class RateLimiter(Protocol):
    """Record calls against a key's window."""

    async def acquire(self, key: str, limit: int) -> float | None:
        """Record one call; return the seconds to wait when the window is full, else ``None``."""
        ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class LocalRateLimiter:
    """In-process sliding windows for a single development process."""

    clock: Callable[[], float] = time.monotonic
    _calls: dict[str, deque[float]] = field(default_factory=dict)

    async def acquire(self, key: str, limit: int) -> float | None:
        """Record one call in ``key``'s window unless ``limit`` calls already fill it."""
        now = self.clock()
        calls = self._calls.setdefault(key, deque())
        while calls and calls[0] <= now - _WINDOW_SECONDS:
            calls.popleft()
        if len(calls) >= limit:
            return calls[0] + _WINDOW_SECONDS - now
        calls.append(now)
        if len(self._calls) > _LOCAL_KEY_CAP:
            self._prune(now)
        return None

    async def aclose(self) -> None:
        """Nothing to release."""

    def _prune(self, now: float) -> None:
        """Forget keys whose window holds no recent call."""
        for key in [
            key for key, calls in self._calls.items() if calls[-1] <= now - _WINDOW_SECONDS
        ]:
            del self._calls[key]


@dataclass(slots=True)
class RedisRateLimiter:
    """Sliding windows in Redis sorted sets, timed by the Redis server clock."""

    client: Any
    key_prefix: str = "harborrag-api-rate"

    async def acquire(self, key: str, limit: int) -> float | None:
        """Record one call in ``key``'s window unless ``limit`` calls already fill it."""
        wait_us = await self.client.eval(
            _ACQUIRE_SCRIPT,
            1,
            f"{self.key_prefix}:{key}",
            limit,
            int(_WINDOW_SECONDS * 1_000_000),
            uuid4().hex,
        )
        return int(wait_us) / 1_000_000 if int(wait_us) > 0 else None

    async def aclose(self) -> None:
        """Close the Redis client."""
        close: Callable[[], Awaitable[None]] | None = getattr(self.client, "aclose", None)
        if close is not None:
            await close()


def build_rate_limiter(redis_url: str | None) -> RateLimiter:
    """Use Redis when configured; keep windows in process otherwise."""
    if redis_url is None:
        return LocalRateLimiter()
    from redis.asyncio import Redis

    return RedisRateLimiter(Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2))


def _key(bucket: RateBucket, principal: Principal) -> str:
    """Bucket and a fixed-length digest of the principal."""
    digest = hashlib.sha256(f"{principal.token_kind}:{principal.subject}".encode()).hexdigest()
    return f"{bucket}:{digest[:32]}"


async def charge(request: Request, principal: Principal, bucket: RateBucket) -> None:
    """Charge this request to ``principal``'s ``bucket`` once, however many guards run.

    Args:
        request: The request being served.
        principal: The caller, already authenticated and authorized.
        bucket: ``chat`` for completions, ``default`` for everything else.

    Raises:
        HarborRateLimitError: The bucket's window is full.
    """
    if getattr(request.state, "rate_charged", False):
        return
    request.state.rate_charged = True
    settings: ApiSettings = request.app.state.settings
    limit = settings.http_rate_chat if bucket == "chat" else settings.http_rate_default
    limiter: RateLimiter = request.app.state.rate_limiter
    try:
        wait = await limiter.acquire(_key(bucket, principal), limit)
    except Exception:  # noqa: BLE001 - an unreachable limiter must not refuse calls
        logger.warning("Rate limiter unavailable; letting the call through", exc_info=True)
        return
    if wait is not None:
        raise HarborRateLimitError(
            f"over the {bucket} limit of {limit} requests per second",
            retry_after_seconds=max(1, math.ceil(wait)),
        )
