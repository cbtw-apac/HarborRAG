from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from .redis_client import RedisConnectionLifecycle
from .routing_state import RoutingAdmissionError, RoutingLease, RoutingStateSnapshot


class RedisRoutingStateStore:
    """Persist distributed routing admission, circuit, and health state in Redis."""

    def __init__(
        self,
        connections: RedisConnectionLifecycle,
        *,
        key_prefix: str,
        owns_connections: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind shared Redis clients and a collision-resistant key namespace."""

        self._connections = connections
        self._prefix = key_prefix.rstrip(":")
        self._owns_connections = owns_connections
        self._clock = clock

    def snapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        """Read a deployment hash and its unexpired lease count from Redis."""

        client = self._connections.sync()
        state = client.hgetall(self._state_key(deployment_key))
        active = client.zcount(self._active_key(deployment_key), self._clock(), "+inf")
        return _snapshot(state, active)

    async def asnapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        """Read a deployment hash and its unexpired lease count asynchronously."""

        client = self._connections.async_client()
        state = await client.hgetall(self._state_key(deployment_key))
        active = await client.zcount(self._active_key(deployment_key), self._clock(), "+inf")
        return _snapshot(state, active)

    def acquire(
        self,
        deployment_key: str,
        *,
        max_parallel: int | None,
        rpm: int | None,
        tpm: int | None,
        token_cost: int,
        lease_seconds: int,
    ) -> RoutingLease:
        """Execute the atomic Redis admission script and return one lease."""

        lease = RoutingLease(deployment_key, uuid.uuid4().hex)
        result = self._connections.sync().eval(
            _ADMISSION_SCRIPT,
            3,
            self._state_key(deployment_key),
            self._rate_key(deployment_key),
            self._active_key(deployment_key),
            self._clock(),
            max_parallel or 0,
            rpm or 0,
            tpm or 0,
            max(0, token_cost),
            lease_seconds,
            lease.lease_id,
        )
        _require_admission(result)
        return lease

    async def aacquire(
        self,
        deployment_key: str,
        *,
        max_parallel: int | None,
        rpm: int | None,
        tpm: int | None,
        token_cost: int,
        lease_seconds: int,
    ) -> RoutingLease:
        """Execute the atomic Redis admission script asynchronously."""

        lease = RoutingLease(deployment_key, uuid.uuid4().hex)
        result = await self._connections.async_client().eval(
            _ADMISSION_SCRIPT,
            3,
            self._state_key(deployment_key),
            self._rate_key(deployment_key),
            self._active_key(deployment_key),
            self._clock(),
            max_parallel or 0,
            rpm or 0,
            tpm or 0,
            max(0, token_cost),
            lease_seconds,
            lease.lease_id,
        )
        _require_admission(result)
        return lease

    def release(self, lease: RoutingLease) -> None:
        """Release a Redis lease idempotently."""

        self._connections.sync().zrem(self._active_key(lease.deployment_key), lease.lease_id)

    async def arelease(self, lease: RoutingLease) -> None:
        """Release a Redis lease asynchronously."""

        await self._connections.async_client().zrem(
            self._active_key(lease.deployment_key), lease.lease_id
        )

    def record_success(self, deployment_key: str, latency_ms: float) -> None:
        """Reset distributed passive failure state."""

        self._connections.sync().eval(
            _SUCCESS_SCRIPT, 1, self._state_key(deployment_key), latency_ms
        )

    async def arecord_success(self, deployment_key: str, latency_ms: float) -> None:
        """Reset distributed passive failure state asynchronously."""

        await self._connections.async_client().eval(
            _SUCCESS_SCRIPT, 1, self._state_key(deployment_key), latency_ms
        )

    def record_failure(
        self,
        deployment_key: str,
        *,
        retryable: bool,
        threshold: int,
        recovery_seconds: float,
    ) -> None:
        """Record a distributed passive failure and open the circuit when required."""

        if retryable:
            self._connections.sync().eval(
                _FAILURE_SCRIPT,
                1,
                self._state_key(deployment_key),
                threshold,
                self._clock(),
                recovery_seconds,
            )

    async def arecord_failure(
        self,
        deployment_key: str,
        *,
        retryable: bool,
        threshold: int,
        recovery_seconds: float,
    ) -> None:
        """Record a distributed passive failure asynchronously."""

        if retryable:
            await self._connections.async_client().eval(
                _FAILURE_SCRIPT,
                1,
                self._state_key(deployment_key),
                threshold,
                self._clock(),
                recovery_seconds,
            )

    def record_active_health(
        self, deployment_key: str, *, healthy: bool, latency_ms: float | None
    ) -> None:
        """Persist one active health result in Redis."""

        self._connections.sync().eval(
            _ACTIVE_HEALTH_SCRIPT,
            1,
            self._state_key(deployment_key),
            1 if healthy else 0,
            self._clock(),
            "" if latency_ms is None else latency_ms,
        )

    async def arecord_active_health(
        self, deployment_key: str, *, healthy: bool, latency_ms: float | None
    ) -> None:
        """Persist one active health result asynchronously."""

        await self._connections.async_client().eval(
            _ACTIVE_HEALTH_SCRIPT,
            1,
            self._state_key(deployment_key),
            1 if healthy else 0,
            self._clock(),
            "" if latency_ms is None else latency_ms,
        )

    def close(self) -> None:
        """Close shared Redis connections only when owned by this store."""

        if self._owns_connections:
            self._connections.close()

    async def aclose(self) -> None:
        """Close owned Redis resources asynchronously."""

        if self._owns_connections:
            await self._connections.aclose()

    def _state_key(self, deployment_key: str) -> str:
        return f"{self._prefix}:route:{deployment_key}:state"

    def _rate_key(self, deployment_key: str) -> str:
        return f"{self._prefix}:route:{deployment_key}:rate"

    def _active_key(self, deployment_key: str) -> str:
        return f"{self._prefix}:route:{deployment_key}:active"


# Active leases live in a sorted set scored by their expiry time. Expired members
# are pruned on every admission and excluded from every read, so a crashed client
# or a request that outlives its lease can never permanently consume a slot.
_ADMISSION_SCRIPT = """
local now = tonumber(ARGV[1])
local max_parallel = tonumber(ARGV[2])
local rpm = tonumber(ARGV[3])
local tpm = tonumber(ARGV[4])
local tokens = tonumber(ARGV[5])
local lease_seconds = tonumber(ARGV[6])
local lease_id = ARGV[7]
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
if open_until > now then return {0, 'circuit_open'} end
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
local active = redis.call('ZCARD', KEYS[3])
if max_parallel > 0 and active >= max_parallel then return {0, 'concurrency'} end
local minute = math.floor(now / 60)
local rate_minute = tonumber(redis.call('HGET', KEYS[2], 'minute') or '-1')
local requests = tonumber(redis.call('HGET', KEYS[2], 'requests') or '0')
local used_tokens = tonumber(redis.call('HGET', KEYS[2], 'tokens') or '0')
if rate_minute ~= minute then requests = 0 used_tokens = 0 end
if rpm > 0 and requests >= rpm then return {0, 'rpm'} end
if tpm > 0 and used_tokens + tokens > tpm then return {0, 'tpm'} end
redis.call('HSET', KEYS[2], 'minute', minute, 'requests', requests + 1,
  'tokens', used_tokens + tokens)
redis.call('EXPIRE', KEYS[2], 120)
redis.call('ZADD', KEYS[3], now + lease_seconds, lease_id)
redis.call('EXPIRE', KEYS[3], lease_seconds)
return {1, 'ok'}
"""
_SUCCESS_SCRIPT = """
redis.call('HSET', KEYS[1], 'failures', 0, 'open_until', 0, 'latency_ms', ARGV[1])
return 1
"""
_FAILURE_SCRIPT = """
local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if failures >= tonumber(ARGV[1]) then
  redis.call('HSET', KEYS[1], 'open_until', tonumber(ARGV[2]) + tonumber(ARGV[3]))
end
return failures
"""
_ACTIVE_HEALTH_SCRIPT = """
redis.call('HSET', KEYS[1], 'active_healthy', ARGV[1], 'active_checked_at', ARGV[2])
if ARGV[3] ~= '' then redis.call('HSET', KEYS[1], 'latency_ms', ARGV[3]) end
return 1
"""


def _snapshot(value: object, active: object) -> RoutingStateSnapshot:
    raw = value if isinstance(value, dict) else {}
    normalized = {
        str(key.decode() if isinstance(key, bytes) else key): (
            item.decode() if isinstance(item, bytes) else item
        )
        for key, item in raw.items()
    }
    healthy = normalized.get("active_healthy")
    return RoutingStateSnapshot(
        active_requests=int(active) if isinstance(active, (int, float, str)) else 0,
        consecutive_failures=int(normalized.get("failures", 0)),
        circuit_open_until=float(normalized.get("open_until", 0)),
        last_latency_ms=_optional_float(normalized.get("latency_ms")),
        active_healthy=None if healthy is None else bool(int(healthy)),
        active_checked_at=_optional_float(normalized.get("active_checked_at")),
    )


def _optional_float(value: object) -> float | None:
    return None if value in (None, "") else float(str(value))


def _require_admission(result: object) -> None:
    values = list(result) if isinstance(result, (list, tuple)) else []
    accepted = bool(values and int(values[0]))
    reason: Any = values[1] if len(values) > 1 else "unknown"
    if isinstance(reason, bytes):
        reason = reason.decode()
    if not accepted:
        raise RoutingAdmissionError(str(reason))
