from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class RoutingAdmissionError(RuntimeError):
    """Report distributed circuit, concurrency, or rate-limit admission rejection."""

    def __init__(self, reason: str) -> None:
        """Store a stable machine-readable admission reason."""

        super().__init__(f"deployment admission rejected: {reason}")
        self.reason = reason


class RoutingStateSnapshot(BaseModel):
    """Expose sanitized distributed deployment health and admission state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_requests: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    circuit_open_until: float = Field(default=0.0, ge=0)
    last_latency_ms: float | None = Field(default=None, ge=0)
    active_healthy: bool | None = None
    active_checked_at: float | None = Field(default=None, ge=0)

    def available(self, now: float, *, health_stale_seconds: float) -> bool:
        """Return whether circuit and active-health state allow new traffic."""

        if now < self.circuit_open_until:
            return False
        if self.active_healthy is not False or self.active_checked_at is None:
            return True
        return now - self.active_checked_at > health_stale_seconds


@dataclass(frozen=True, slots=True)
class RoutingLease:
    """Identify one distributed concurrency permit that must be released once."""

    deployment_key: str
    lease_id: str


class RoutingStateStore(Protocol):
    """Define distributed circuit, admission, rate, and health state operations."""

    def snapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        """Read the current deployment state synchronously."""

        ...

    async def asnapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        """Read the current deployment state asynchronously."""

        ...

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
        """Atomically acquire circuit, concurrency, and fixed-window rate admission."""

        ...

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
        """Atomically acquire distributed admission asynchronously."""

        ...

    def release(self, lease: RoutingLease) -> None:
        """Release one distributed concurrency lease."""

        ...

    async def arelease(self, lease: RoutingLease) -> None:
        """Release one distributed concurrency lease asynchronously."""

        ...

    def record_success(self, deployment_key: str, latency_ms: float) -> None:
        """Record passive success and latency."""

        ...

    async def arecord_success(self, deployment_key: str, latency_ms: float) -> None:
        """Record passive success and latency asynchronously."""

        ...

    def record_failure(
        self,
        deployment_key: str,
        *,
        retryable: bool,
        threshold: int,
        recovery_seconds: float,
    ) -> None:
        """Record passive failure and open the distributed circuit when required."""

        ...

    async def arecord_failure(
        self,
        deployment_key: str,
        *,
        retryable: bool,
        threshold: int,
        recovery_seconds: float,
    ) -> None:
        """Record passive failure asynchronously."""

        ...

    def record_active_health(
        self, deployment_key: str, *, healthy: bool, latency_ms: float | None
    ) -> None:
        """Record one active probe result."""

        ...

    async def arecord_active_health(
        self, deployment_key: str, *, healthy: bool, latency_ms: float | None
    ) -> None:
        """Record one active probe result asynchronously."""

        ...

    def close(self) -> None:
        """Release owned state-store resources."""

        ...

    async def aclose(self) -> None:
        """Release owned state-store resources asynchronously."""

        ...
