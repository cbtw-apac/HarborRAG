"""Shared connector rate-limit policies with a disposable Redis projection."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger("harborrag.adapters.connectors.rate_limiting")

_SAFE_KEY_PART = re.compile(r"[^a-z0-9._-]+")
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local clock = redis.call("TIME")
local now_ms = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
local state = redis.call("HMGET", key, "tokens", "updated_ms")
local tokens
local updated_ms

if state[1] == false then
    tokens = math.min(capacity, 1)
    updated_ms = now_ms
else
    tokens = tonumber(state[1])
    updated_ms = tonumber(state[2])
    tokens = math.min(capacity, tokens + math.max(0, now_ms - updated_ms) * refill_per_ms)
end

local allowed = 0
local wait_ms = 0
if tokens >= 1 then
    allowed = 1
    tokens = tokens - 1
else
    wait_ms = math.ceil((1 - tokens) / refill_per_ms)
end

redis.call("HSET", key, "tokens", tostring(tokens), "updated_ms", tostring(now_ms))
redis.call("PEXPIRE", key, ttl_ms)
return {allowed, wait_ms}
"""

type RateLimitWaitObserver = Callable[["RateLimitScope", float], None]


class RedisScriptClient(Protocol):
    """Minimal redis-py surface required by the token bucket."""

    def eval(
        self,
        script: str,
        number_of_keys: int,
        *keys_and_args: str,
    ) -> object:
        """Evaluate one Lua script."""

    def close(self) -> Any:
        """Release client connections."""


@dataclass(frozen=True, slots=True)
class RateLimitIdentity:
    """Stable, secret-safe dimensions shared by one connector credential."""

    connector_type: str
    deployment_type: str
    source_host: str
    credential_identity: str

    @classmethod
    def from_http_source(
        cls,
        *,
        connector_type: str,
        deployment_type: str,
        base_url: str,
        credential_parts: tuple[str, ...],
    ) -> RateLimitIdentity:
        """Build an identity without retaining raw credentials."""

        parsed = urlsplit(base_url)
        host = (parsed.hostname or "unknown-host").lower()
        if parsed.port is not None:
            host = f"{host}-{parsed.port}"
        credential_digest = sha256("\0".join(credential_parts).encode("utf-8")).hexdigest()[:24]
        return cls(
            connector_type=_safe_key_part(connector_type),
            deployment_type=_safe_key_part(deployment_type),
            source_host=_safe_key_part(host),
            credential_identity=credential_digest,
        )

    def scope(self, api_family: str) -> RateLimitScope:
        """Add the controlled API-family lane used for one request."""

        return RateLimitScope(
            connector_type=self.connector_type,
            deployment_type=self.deployment_type,
            source_host=self.source_host,
            credential_identity=self.credential_identity,
            api_family=_safe_key_part(api_family),
        )


@dataclass(frozen=True, slots=True)
class RateLimitScope:
    """Complete token-bucket key dimensions for one API family."""

    connector_type: str
    deployment_type: str
    source_host: str
    credential_identity: str
    api_family: str

    def redis_key(self, prefix: str) -> str:
        """Return a readable key containing only sanitized, non-secret values."""

        dimensions = (
            self.connector_type,
            self.deployment_type,
            self.source_host,
            self.credential_identity,
            self.api_family,
        )
        return ":".join((_safe_key_part(prefix), *dimensions))


class ConnectorRateLimiter(Protocol):
    """Blocking rate-limit boundary used by synchronous HTTP connectors."""

    def acquire(
        self,
        scope: RateLimitScope,
        *,
        requests_per_minute: int,
    ) -> None:
        """Wait until the connector may issue one request."""

    def close(self) -> None:
        """Release limiter resources."""


class LocalIntervalRateLimiter:
    """Process-local conservative fallback with thread-safe slot reservation."""

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        on_wait: RateLimitWaitObserver | None = None,
    ) -> None:
        self._sleep = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._on_wait = on_wait
        self._next_slots: dict[RateLimitScope, float] = {}
        self._lock = Lock()

    def acquire(
        self,
        scope: RateLimitScope,
        *,
        requests_per_minute: int,
    ) -> None:
        """Reserve one evenly-spaced local request slot."""

        interval = 60.0 / requests_per_minute
        now = self._clock()
        with self._lock:
            scheduled = max(now, self._next_slots.get(scope, now))
            self._next_slots[scope] = scheduled + interval
        wait_seconds = scheduled - now
        if wait_seconds > 0:
            _notify_wait(self._on_wait, scope, wait_seconds)
            self._sleep(wait_seconds)

    def close(self) -> None:
        """Discard disposable local scheduling state."""

        with self._lock:
            self._next_slots.clear()


class RedisTokenBucketRateLimiter:
    """Distributed Lua token bucket that falls back locally on Redis loss."""

    def __init__(
        self,
        client: RedisScriptClient,
        *,
        key_prefix: str = "harborrag-connector-rate",
        fallback: ConnectorRateLimiter | None = None,
        sleeper: Callable[[float], None] | None = None,
        on_wait: RateLimitWaitObserver | None = None,
    ) -> None:
        self._client = client
        self._key_prefix = _safe_key_part(key_prefix)
        self._fallback = fallback or LocalIntervalRateLimiter(on_wait=on_wait)
        self._sleep = sleeper or time.sleep
        self._on_wait = on_wait
        self._redis_available = True

    def acquire(
        self,
        scope: RateLimitScope,
        *,
        requests_per_minute: int,
    ) -> None:
        """Acquire a distributed token or use local conservative throttling."""

        try:
            self._acquire_from_redis(
                scope,
                requests_per_minute=requests_per_minute,
            )
            self._redis_available = True
        except Exception as error:
            if self._redis_available:
                logger.warning(
                    "Redis connector rate limiter unavailable; using local fallback (%s)",
                    type(error).__name__,
                )
            self._redis_available = False
            self._fallback.acquire(
                scope,
                requests_per_minute=requests_per_minute,
            )

    def close(self) -> None:
        """Close Redis connections and discard fallback state."""

        try:
            self._client.close()
        finally:
            self._fallback.close()

    def _acquire_from_redis(
        self,
        scope: RateLimitScope,
        *,
        requests_per_minute: int,
    ) -> None:
        capacity = requests_per_minute
        refill_per_ms = requests_per_minute / 60_000
        ttl_ms = 120_000
        key = scope.redis_key(self._key_prefix)
        while True:
            result = self._client.eval(
                _TOKEN_BUCKET_LUA,
                1,
                key,
                str(capacity),
                repr(refill_per_ms),
                str(ttl_ms),
            )
            allowed, wait_ms = _parse_lua_result(result)
            if allowed:
                return
            wait_seconds = max(wait_ms, 1) / 1000
            _notify_wait(self._on_wait, scope, wait_seconds)
            self._sleep(wait_seconds)


def _parse_lua_result(result: object) -> tuple[bool, int]:
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise ValueError("Redis token bucket returned an invalid response")
    return bool(int(result[0])), max(0, int(result[1]))


def _safe_key_part(value: str) -> str:
    normalized = _SAFE_KEY_PART.sub("-", value.strip().lower()).strip("-")
    return normalized or "unknown"


def _notify_wait(
    observer: RateLimitWaitObserver | None,
    scope: RateLimitScope,
    wait_seconds: float,
) -> None:
    if observer is None:
        return
    try:
        observer(scope, wait_seconds)
    except Exception as error:
        logger.warning(
            "Connector rate-limit observer failed (%s)",
            type(error).__name__,
        )
