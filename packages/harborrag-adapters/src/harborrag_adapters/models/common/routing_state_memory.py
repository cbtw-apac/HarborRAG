from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from .routing_state import RoutingAdmissionError, RoutingLease, RoutingStateSnapshot


@dataclass(slots=True)
class _MemoryState:
    active: int = 0
    failures: int = 0
    open_until: float = 0.0
    latency_ms: float | None = None
    active_healthy: bool | None = None
    active_checked_at: float | None = None
    rate_minute: int = -1
    requests: int = 0
    tokens: int = 0


class InMemoryRoutingStateStore:
    """Provide process-local semantics equivalent to the Redis routing store."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        """Initialize thread-safe deployment state using the supplied wall clock."""

        self._clock = clock
        self._states: dict[str, _MemoryState] = {}
        self._leases: set[tuple[str, str]] = set()
        self._lock = RLock()

    def snapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        """Return one immutable state snapshot."""

        with self._lock:
            state = self._states.setdefault(deployment_key, _MemoryState())
            return RoutingStateSnapshot(
                active_requests=state.active,
                consecutive_failures=state.failures,
                circuit_open_until=state.open_until,
                last_latency_ms=state.latency_ms,
                active_healthy=state.active_healthy,
                active_checked_at=state.active_checked_at,
            )

    async def asnapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        """Return one state snapshot asynchronously."""

        return self.snapshot(deployment_key)

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
        """Apply atomic local circuit, concurrency, RPM, and TPM admission."""

        del lease_seconds
        with self._lock:
            now = self._clock()
            state = self._states.setdefault(deployment_key, _MemoryState())
            if state.open_until > now:
                raise RoutingAdmissionError("circuit_open")
            self._reset_rate_window(state, now)
            if max_parallel is not None and state.active >= max_parallel:
                raise RoutingAdmissionError("concurrency")
            if rpm is not None and state.requests >= rpm:
                raise RoutingAdmissionError("rpm")
            if tpm is not None and state.tokens + token_cost > tpm:
                raise RoutingAdmissionError("tpm")
            lease = RoutingLease(deployment_key, uuid.uuid4().hex)
            state.active += 1
            state.requests += 1
            state.tokens += max(0, token_cost)
            self._leases.add((deployment_key, lease.lease_id))
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
        """Apply local admission asynchronously without blocking the event loop."""

        return self.acquire(
            deployment_key,
            max_parallel=max_parallel,
            rpm=rpm,
            tpm=tpm,
            token_cost=token_cost,
            lease_seconds=lease_seconds,
        )

    def release(self, lease: RoutingLease) -> None:
        """Release one local lease exactly once."""

        with self._lock:
            marker = (lease.deployment_key, lease.lease_id)
            if marker not in self._leases:
                return
            self._leases.remove(marker)
            state = self._states.setdefault(lease.deployment_key, _MemoryState())
            state.active = max(0, state.active - 1)

    async def arelease(self, lease: RoutingLease) -> None:
        """Release one local lease asynchronously."""

        self.release(lease)

    def record_success(self, deployment_key: str, latency_ms: float) -> None:
        """Reset passive failures and record latency."""

        with self._lock:
            state = self._states.setdefault(deployment_key, _MemoryState())
            state.failures = 0
            state.open_until = 0.0
            state.latency_ms = latency_ms

    async def arecord_success(self, deployment_key: str, latency_ms: float) -> None:
        """Record passive success asynchronously."""

        self.record_success(deployment_key, latency_ms)

    def record_failure(
        self,
        deployment_key: str,
        *,
        retryable: bool,
        threshold: int,
        recovery_seconds: float,
    ) -> None:
        """Increment passive failures and open the circuit at the configured threshold."""

        if not retryable:
            return
        with self._lock:
            state = self._states.setdefault(deployment_key, _MemoryState())
            state.failures += 1
            if state.failures >= threshold:
                state.open_until = self._clock() + recovery_seconds

    async def arecord_failure(
        self,
        deployment_key: str,
        *,
        retryable: bool,
        threshold: int,
        recovery_seconds: float,
    ) -> None:
        """Record passive failure asynchronously."""

        self.record_failure(
            deployment_key,
            retryable=retryable,
            threshold=threshold,
            recovery_seconds=recovery_seconds,
        )

    def record_active_health(
        self, deployment_key: str, *, healthy: bool, latency_ms: float | None
    ) -> None:
        """Record active health and optional probe latency."""

        with self._lock:
            state = self._states.setdefault(deployment_key, _MemoryState())
            state.active_healthy = healthy
            state.active_checked_at = self._clock()
            if latency_ms is not None:
                state.latency_ms = latency_ms

    async def arecord_active_health(
        self, deployment_key: str, *, healthy: bool, latency_ms: float | None
    ) -> None:
        """Record active health asynchronously."""

        self.record_active_health(deployment_key, healthy=healthy, latency_ms=latency_ms)

    def close(self) -> None:
        """Clear local state."""

        with self._lock:
            self._states.clear()
            self._leases.clear()

    async def aclose(self) -> None:
        """Clear local state asynchronously."""

        self.close()

    @staticmethod
    def _reset_rate_window(state: _MemoryState, now: float) -> None:
        minute = int(now // 60)
        if minute != state.rate_minute:
            state.rate_minute = minute
            state.requests = 0
            state.tokens = 0
