"""Connector rate limiting with a Redis token bucket and a local fallback.

Handles:
- Secret-safe rate-limit identities and scopes
- Process-local interval limiter
- Redis Lua token bucket that falls back locally when Redis fails
"""

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
    """Subset of the redis-py client used by the token bucket."""

    def eval(
        self,
        script: str,
        number_of_keys: int,
        *keys_and_args: str,
    ) -> object:
        """Evaluate a Lua script with the given keys and arguments."""

    def close(self) -> Any:
        """Close the client connections."""


@dataclass(frozen=True, slots=True)
class RateLimitIdentity:
    """Secret-safe dimensions shared by all requests of one connector credential."""

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
        """Build an identity from an HTTP source, keeping only a credential digest.

        Args:
            connector_type: Provider name, such as ``jira``.
            deployment_type: Deployment flavor, such as ``cloud``.
            base_url: Source base URL; only its host and port are kept.
            credential_parts: Values that identify the credential; hashed, never stored.
        """
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
        """Return the scope for one API family under this identity."""
        return RateLimitScope(
            connector_type=self.connector_type,
            deployment_type=self.deployment_type,
            source_host=self.source_host,
            credential_identity=self.credential_identity,
            api_family=_safe_key_part(api_family),
        )


@dataclass(frozen=True, slots=True)
class RateLimitScope:
    """Token-bucket key dimensions for one API family of one credential."""

    connector_type: str
    deployment_type: str
    source_host: str
    credential_identity: str
    api_family: str

    def redis_key(self, prefix: str) -> str:
        """Return the Redis key: sanitized prefix followed by the scope dimensions."""
        dimensions = (
            self.connector_type,
            self.deployment_type,
            self.source_host,
            self.credential_identity,
            self.api_family,
        )
        return ":".join((_safe_key_part(prefix), *dimensions))


class ConnectorRateLimiter(Protocol):
    """Blocking rate limiter used by synchronous HTTP connectors."""

    def acquire(
        self,
        scope: RateLimitScope,
        *,
        requests_per_minute: int,
    ) -> None:
        """Block until one request is allowed for ``scope`` at ``requests_per_minute``."""

    def close(self) -> None:
        """Release limiter resources."""


class LocalIntervalRateLimiter:
    """Process-local limiter that spaces requests evenly per scope.

    Thread-safe; state is not shared across processes.
    """

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        on_wait: RateLimitWaitObserver | None = None,
    ) -> None:
        """Set up per-scope slot tracking.

        Args:
            sleeper: Sleep function; defaults to ``time.sleep``.
            clock: Monotonic clock; defaults to ``time.monotonic``.
            on_wait: Observer called with the scope and wait before sleeping.
        """
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
        """Reserve the next evenly spaced slot for ``scope`` and sleep until it starts."""
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
        """Discard the local slot state."""
        with self._lock:
            self._next_slots.clear()


class RedisTokenBucketRateLimiter:
    """Distributed Lua token bucket that falls back to a local limiter on Redis errors."""

    def __init__(
        self,
        client: RedisScriptClient,
        *,
        key_prefix: str = "harborrag-connector-rate",
        fallback: ConnectorRateLimiter | None = None,
        sleeper: Callable[[float], None] | None = None,
        on_wait: RateLimitWaitObserver | None = None,
    ) -> None:
        """Bind the Redis client and the fallback limiter.

        ``close`` closes both the client and the fallback.

        Args:
            client: Redis client used to run the Lua script.
            key_prefix: Redis key prefix; sanitized.
            fallback: Limiter used while Redis fails; defaults to ``LocalIntervalRateLimiter``.
            sleeper: Sleep function; defaults to ``time.sleep``.
            on_wait: Observer called with the scope and wait before sleeping.
        """
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
        """Take a token from Redis, or from the local fallback if Redis fails."""
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
        """Close the Redis client and the fallback limiter."""
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
        """Run the Lua token bucket, sleeping and retrying until a token is granted.

        The bucket refills continuously up to ``requests_per_minute`` tokens; a new bucket
        starts with one token and its key expires after 120 seconds idle. Errors propagate.
        """
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
    """Return ``(allowed, wait_ms)`` from the script reply. Raises ``ValueError`` if malformed."""
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise ValueError("Redis token bucket returned an invalid response")
    return bool(int(result[0])), max(0, int(result[1]))


def _safe_key_part(value: str) -> str:
    """Lowercase a key part, replacing runs outside ``[a-z0-9._-]`` with ``-``.

    An empty result becomes ``unknown``.
    """
    normalized = _SAFE_KEY_PART.sub("-", value.strip().lower()).strip("-")
    return normalized or "unknown"


def _notify_wait(
    observer: RateLimitWaitObserver | None,
    scope: RateLimitScope,
    wait_seconds: float,
) -> None:
    """Call the wait observer, logging its errors instead of raising."""
    if observer is None:
        return
    try:
        observer(scope, wait_seconds)
    except Exception as error:
        logger.warning(
            "Connector rate-limit observer failed (%s)",
            type(error).__name__,
        )
