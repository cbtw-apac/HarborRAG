from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore, RLock
from typing import Protocol

from .config import CircuitBreakerConfig, RoutingStrategy


class DeploymentLike(Protocol):
    """Expose selection and concurrency fields shared by provider deployments."""

    name: str
    enabled: bool
    weight: float
    order: int
    max_parallel_requests: int | None


class NoHealthyDeploymentError(RuntimeError):
    """Report that routing has no enabled, healthy deployment candidate."""


@dataclass(slots=True)
class DeploymentRuntime[D: DeploymentLike]:
    """Track mutable health and concurrency state for one deployment."""

    config: D
    active_requests: int = 0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    last_latency_ms: float | None = None
    semaphore: BoundedSemaphore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.config.max_parallel_requests:
            self.semaphore = BoundedSemaphore(self.config.max_parallel_requests)

    def available(self, now: float) -> bool:
        return self.config.enabled and now >= self.circuit_open_until


class DeploymentSelector[D: DeploymentLike]:
    """Select and concurrency-limit deployments for sync and async clients."""

    def __init__(
        self,
        deployments: dict[str, Sequence[D]],
        *,
        strategy: RoutingStrategy,
        circuit_breaker: CircuitBreakerConfig,
        enable_health_tracking: bool,
        random_source: random.Random | None = None,
    ) -> None:
        self._strategy = strategy
        self._circuit = circuit_breaker
        self._enable_health_tracking = enable_health_tracking
        self._random = random_source or random.Random()
        self._states = {
            (logical, deployment.name): DeploymentRuntime(deployment)
            for logical, items in deployments.items()
            for deployment in items
        }
        self._round_robin_index: dict[str, int] = {}
        self._lock = RLock()

    def select_sync(
        self, logical_model: str, deployments: Sequence[D], *, exclude: Iterable[str] = ()
    ) -> DeploymentRuntime[D]:
        excluded = set(exclude)
        with self._lock:
            now = time.monotonic()
            candidates = [
                self._states[(logical_model, deployment.name)]
                for deployment in deployments
                if deployment.name not in excluded
                and self._states[(logical_model, deployment.name)].available(now)
            ]
            if not candidates:
                raise NoHealthyDeploymentError("no healthy deployments are available")
            candidates.sort(key=lambda state: (state.config.order, state.config.name))
            priority = [c for c in candidates if c.config.order == candidates[0].config.order]
            if self._strategy is RoutingStrategy.ORDERED:
                return priority[0]
            if self._strategy is RoutingStrategy.LEAST_BUSY:
                return min(priority, key=lambda state: (state.active_requests, state.config.name))
            if self._strategy is RoutingStrategy.LATENCY:
                return min(
                    priority,
                    key=lambda state: (
                        float("inf") if state.last_latency_ms is None else state.last_latency_ms,
                        state.active_requests,
                    ),
                )
            if self._strategy is RoutingStrategy.ROUND_ROBIN:
                index = self._round_robin_index.get(logical_model, 0)
                self._round_robin_index[logical_model] = index + 1
                return priority[index % len(priority)]
            return self._random.choices(
                priority, weights=[state.config.weight for state in priority], k=1
            )[0]

    async def select(
        self, logical_model: str, deployments: Sequence[D], *, exclude: Iterable[str] = ()
    ) -> DeploymentRuntime[D]:
        return self.select_sync(logical_model, deployments, exclude=exclude)

    @contextmanager
    def lease_sync(self, state: DeploymentRuntime[D]) -> Iterator[None]:
        if state.semaphore is not None:
            state.semaphore.acquire()
        with self._lock:
            state.active_requests += 1
        try:
            yield
        finally:
            with self._lock:
                state.active_requests = max(0, state.active_requests - 1)
            if state.semaphore is not None:
                state.semaphore.release()

    @asynccontextmanager
    async def lease(self, state: DeploymentRuntime[D]) -> AsyncIterator[None]:
        if state.semaphore is not None:
            await asyncio.to_thread(state.semaphore.acquire)
        with self._lock:
            state.active_requests += 1
        try:
            yield
        finally:
            with self._lock:
                state.active_requests = max(0, state.active_requests - 1)
            if state.semaphore is not None:
                state.semaphore.release()

    def record_success_sync(self, state: DeploymentRuntime[D], latency_ms: float) -> None:
        if self._enable_health_tracking:
            with self._lock:
                state.consecutive_failures = 0
                state.circuit_open_until = 0.0
                state.last_latency_ms = latency_ms

    async def record_success(self, state: DeploymentRuntime[D], latency_ms: float) -> None:
        self.record_success_sync(state, latency_ms)

    def record_failure_sync(self, state: DeploymentRuntime[D], *, retryable: bool) -> None:
        if not self._enable_health_tracking or not retryable or not self._circuit.enabled:
            return
        with self._lock:
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._circuit.failure_threshold:
                state.circuit_open_until = time.monotonic() + self._circuit.recovery_timeout_seconds

    async def record_failure(self, state: DeploymentRuntime[D], *, retryable: bool) -> None:
        self.record_failure_sync(state, retryable=retryable)
