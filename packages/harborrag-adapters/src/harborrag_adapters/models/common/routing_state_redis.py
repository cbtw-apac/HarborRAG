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
        """Read a deployment hash from Redis."""

        return _snapshot(self._connections.sync().hgetall(self._state_key(deployment_key)))

    async def asnapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        """Read a deployment hash asynchronously."""

        value = await self._connections.async_client().hgetall(self._state_key(deployment_key))
        return _snapshot(value)

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
            self._lease_key(lease),
            self._clock(),
            max_parallel or 0,
            rpm or 0,
            tpm or 0,
            max(0, token_cost),
            lease_seconds,
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
            self._lease_key(lease),
            self._clock(),
            max_parallel or 0,
            rpm or 0,
            tpm or 0,
            max(0, token_cost),
            lease_seconds,
        )
        _require_admission(result)
        return lease

    def release(self, lease: RoutingLease) -> None:
        """Release a Redis lease idempotently."""

        self._connections.sync().eval(
            _RELEASE_SCRIPT,
            2,
            self._state_key(lease.deployment_key),
            self._lease_key(lease),
        )

    async def arelease(self, lease: RoutingLease) -> None:
        """Release a Redis lease asynchronously."""

        await self._connections.async_client().eval(
            _RELEASE_SCRIPT,
            2,
            self._state_key(lease.deployment_key),
            self._lease_key(lease),
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

    def _lease_key(self, lease: RoutingLease) -> str:
        return f"{self._prefix}:route:{lease.deployment_key}:lease:{lease.lease_id}"


_ADMISSION_SCRIPT = """
local now = tonumber(ARGV[1])
local max_parallel = tonumber(ARGV[2])
local rpm = tonumber(ARGV[3])
local tpm = tonumber(ARGV[4])
local tokens = tonumber(ARGV[5])
local lease_seconds = tonumber(ARGV[6])
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
if open_until > now then return {0, 'circuit_open'} end
local active = tonumber(redis.call('HGET', KEYS[1], 'active') or '0')
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
redis.call('HINCRBY', KEYS[1], 'active', 1)
redis.call('SET', KEYS[3], '1', 'EX', lease_seconds)
return {1, 'ok'}
"""
_RELEASE_SCRIPT = """
if redis.call('DEL', KEYS[2]) == 1 then
  local active = tonumber(redis.call('HGET', KEYS[1], 'active') or '0')
  if active > 0 then redis.call('HINCRBY', KEYS[1], 'active', -1) end
end
return 1
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


def _snapshot(value: object) -> RoutingStateSnapshot:
    raw = value if isinstance(value, dict) else {}
    normalized = {
        str(key.decode() if isinstance(key, bytes) else key): (
            item.decode() if isinstance(item, bytes) else item
        )
        for key, item in raw.items()
    }
    healthy = normalized.get("active_healthy")
    return RoutingStateSnapshot(
        active_requests=int(normalized.get("active", 0)),
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
