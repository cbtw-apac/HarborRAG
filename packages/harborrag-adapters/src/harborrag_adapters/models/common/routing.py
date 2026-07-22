from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Generator, Iterable, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore, RLock
from typing import Protocol
from weakref import WeakKeyDictionary

from .config import CircuitBreakerConfig, RoutingStrategy
from .health import deployment_state_key
from .routing_state import (
    RoutingLease,
    RoutingStateSnapshot,
    RoutingStateStore,
)
from .routing_state_memory import InMemoryRoutingStateStore


class DeploymentLike(Protocol):
    """Expose selection and concurrency fields shared by provider deployments."""

    name: str
    enabled: bool
    weight: float
    order: int
    max_parallel_requests: int | None
    rpm: int | None
    tpm: int | None


class NoHealthyDeploymentError(RuntimeError):
    """Report that routing has no enabled, healthy deployment candidate."""


@dataclass(slots=True)
class DeploymentRuntime[D: DeploymentLike]:
    """Track mutable health and concurrency state for one deployment."""

    config: D
    logical_model: str = ""
    active_requests: int = 0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    last_latency_ms: float | None = None
    distributed_active_requests: int = 0
    sync_semaphore: BoundedSemaphore | None = field(default=None, repr=False)
    _async_semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.BoundedSemaphore] = (
        field(default_factory=WeakKeyDictionary, repr=False)
    )

    def __post_init__(self) -> None:
        if self.config.max_parallel_requests:
            self.sync_semaphore = BoundedSemaphore(self.config.max_parallel_requests)

    def async_semaphore(self) -> asyncio.BoundedSemaphore | None:
        """Return the concurrency semaphore bound to the running event loop."""

        if not self.config.max_parallel_requests:
            return None
        loop = asyncio.get_running_loop()
        semaphore = self._async_semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.BoundedSemaphore(self.config.max_parallel_requests)
            self._async_semaphores[loop] = semaphore
        return semaphore

    def available(self, now: float) -> bool:
        """Return whether the deployment is enabled and outside cooldown."""

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
        state_store: RoutingStateStore | None = None,
        lease_seconds: int = 120,
        health_stale_seconds: float = 90.0,
    ) -> None:
        """Build immutable deployment state and strategy-specific selector state."""

        self._strategy = strategy
        self._circuit = circuit_breaker
        self._enable_health_tracking = enable_health_tracking
        self._random = random_source or random.Random()
        self.state_store = state_store or InMemoryRoutingStateStore()
        self._lease_seconds = lease_seconds
        self._health_stale_seconds = health_stale_seconds
        self._states = {
            (logical, deployment.name): DeploymentRuntime(deployment, logical)
            for logical, items in deployments.items()
            for deployment in items
        }
        self._round_robin_index: dict[str, int] = {}
        self._lock = RLock()

    def select_sync(
        self,
        logical_model: str,
        deployments: Sequence[D],
        *,
        exclude: Iterable[str] = (),
    ) -> DeploymentRuntime[D]:
        """Select one healthy deployment using local and distributed state."""

        excluded = set(exclude)
        with self._lock:
            now = time.time()
            candidates: list[DeploymentRuntime[D]] = []
            for deployment in deployments:
                state = self._states[(logical_model, deployment.name)]
                if deployment.name in excluded or not state.available(time.monotonic()):
                    continue
                snapshot = self.state_store.snapshot(
                    deployment_state_key(logical_model, deployment.name)
                )
                if not snapshot.available(now, health_stale_seconds=self._health_stale_seconds):
                    continue
                self._apply_snapshot(state, snapshot)
                candidates.append(state)
            return self._choose(logical_model, candidates)

    async def select(
        self,
        logical_model: str,
        deployments: Sequence[D],
        *,
        exclude: Iterable[str] = (),
    ) -> DeploymentRuntime[D]:
        """Select one healthy deployment using asynchronous distributed state reads."""

        excluded = set(exclude)
        now = time.time()
        observed: list[tuple[DeploymentRuntime[D], RoutingStateSnapshot]] = []
        for deployment in deployments:
            state = self._states[(logical_model, deployment.name)]
            if deployment.name in excluded or not state.available(time.monotonic()):
                continue
            snapshot = await self.state_store.asnapshot(
                deployment_state_key(logical_model, deployment.name)
            )
            observed.append((state, snapshot))
        with self._lock:
            candidates: list[DeploymentRuntime[D]] = []
            for state, snapshot in observed:
                if not snapshot.available(now, health_stale_seconds=self._health_stale_seconds):
                    continue
                self._apply_snapshot(state, snapshot)
                candidates.append(state)
            return self._choose(logical_model, candidates)

    @contextmanager
    def lease_sync(
        self,
        state: DeploymentRuntime[D],
        *,
        logical_model: str | None = None,
        token_cost: int = 0,
    ) -> Generator[None, None, None]:
        """Acquire local and distributed concurrency and rate-limit admission."""

        if state.sync_semaphore is not None:
            state.sync_semaphore.acquire()
        lease: RoutingLease | None = None
        incremented = False
        try:
            lease = self.state_store.acquire(
                deployment_state_key(logical_model or state.logical_model, state.config.name),
                max_parallel=state.config.max_parallel_requests,
                rpm=getattr(state.config, "rpm", None),
                tpm=getattr(state.config, "tpm", None),
                token_cost=token_cost,
                lease_seconds=self._lease_seconds,
            )
            with self._lock:
                state.active_requests += 1
                incremented = True
            yield
        finally:
            if lease is not None:
                self.state_store.release(lease)
            if incremented:
                with self._lock:
                    state.active_requests = max(0, state.active_requests - 1)
            if state.sync_semaphore is not None:
                state.sync_semaphore.release()

    @asynccontextmanager
    async def lease(
        self,
        state: DeploymentRuntime[D],
        *,
        logical_model: str | None = None,
        token_cost: int = 0,
    ) -> AsyncIterator[None]:
        """Acquire cancellation-safe local and distributed asynchronous admission."""

        semaphore = state.async_semaphore()
        acquired_local = False
        lease: RoutingLease | None = None
        incremented = False
        try:
            if semaphore is not None:
                await semaphore.acquire()
                acquired_local = True
            lease = await self.state_store.aacquire(
                deployment_state_key(logical_model or state.logical_model, state.config.name),
                max_parallel=state.config.max_parallel_requests,
                rpm=getattr(state.config, "rpm", None),
                tpm=getattr(state.config, "tpm", None),
                token_cost=token_cost,
                lease_seconds=self._lease_seconds,
            )
            with self._lock:
                state.active_requests += 1
                incremented = True
            yield
        finally:
            if lease is not None:
                await self.state_store.arelease(lease)
            if incremented:
                with self._lock:
                    state.active_requests = max(0, state.active_requests - 1)
            if acquired_local and semaphore is not None:
                semaphore.release()

    def record_success_sync(self, state: DeploymentRuntime[D], latency_ms: float) -> None:
        """Reset failure state and record successful deployment latency."""

        if self._enable_health_tracking:
            with self._lock:
                state.consecutive_failures = 0
                state.circuit_open_until = 0.0
                state.last_latency_ms = latency_ms
        self.state_store.record_success(
            deployment_state_key(state.logical_model, state.config.name),
            latency_ms,
        )

    async def record_success(self, state: DeploymentRuntime[D], latency_ms: float) -> None:
        """Record asynchronous deployment success using shared health state."""

        if self._enable_health_tracking:
            with self._lock:
                state.consecutive_failures = 0
                state.circuit_open_until = 0.0
                state.last_latency_ms = latency_ms
        await self.state_store.arecord_success(
            deployment_state_key(state.logical_model, state.config.name),
            latency_ms,
        )

    def record_failure_sync(self, state: DeploymentRuntime[D], *, retryable: bool) -> None:
        """Update circuit-breaker state for one retryable deployment failure."""

        if not self._enable_health_tracking or not retryable or not self._circuit.enabled:
            return
        with self._lock:
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._circuit.failure_threshold:
                state.circuit_open_until = time.monotonic() + self._circuit.recovery_timeout_seconds
        self.state_store.record_failure(
            deployment_state_key(state.logical_model, state.config.name),
            retryable=retryable,
            threshold=self._circuit.failure_threshold,
            recovery_seconds=self._circuit.recovery_timeout_seconds,
        )

    async def record_failure(self, state: DeploymentRuntime[D], *, retryable: bool) -> None:
        """Record asynchronous deployment failure using shared health state."""

        if not self._enable_health_tracking or not retryable or not self._circuit.enabled:
            return
        with self._lock:
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._circuit.failure_threshold:
                state.circuit_open_until = time.monotonic() + self._circuit.recovery_timeout_seconds
        await self.state_store.arecord_failure(
            deployment_state_key(state.logical_model, state.config.name),
            retryable=retryable,
            threshold=self._circuit.failure_threshold,
            recovery_seconds=self._circuit.recovery_timeout_seconds,
        )

    def snapshots(self) -> dict[str, RoutingStateSnapshot]:
        """Return distributed state snapshots for runtime introspection."""

        return {
            deployment_state_key(logical, name): self.state_store.snapshot(
                deployment_state_key(logical, name)
            )
            for logical, name in self._states
        }

    async def asnapshots(self) -> dict[str, RoutingStateSnapshot]:
        """Return distributed state snapshots asynchronously."""

        result: dict[str, RoutingStateSnapshot] = {}
        for logical, name in self._states:
            key = deployment_state_key(logical, name)
            result[key] = await self.state_store.asnapshot(key)
        return result

    def _choose(
        self, logical_model: str, candidates: list[DeploymentRuntime[D]]
    ) -> DeploymentRuntime[D]:
        if not candidates:
            raise NoHealthyDeploymentError("no healthy deployments are available")
        candidates.sort(key=lambda state: (state.config.order, state.config.name))
        priority = [c for c in candidates if c.config.order == candidates[0].config.order]
        if self._strategy is RoutingStrategy.ORDERED:
            return priority[0]
        if self._strategy is RoutingStrategy.LEAST_BUSY:
            return min(
                priority,
                key=lambda state: (
                    state.active_requests + state.distributed_active_requests,
                    state.config.name,
                ),
            )
        if self._strategy is RoutingStrategy.LATENCY:
            return min(
                priority,
                key=lambda state: (
                    (float("inf") if state.last_latency_ms is None else state.last_latency_ms),
                    state.active_requests + state.distributed_active_requests,
                ),
            )
        if self._strategy is RoutingStrategy.ROUND_ROBIN:
            index = self._round_robin_index.get(logical_model, 0)
            self._round_robin_index[logical_model] = index + 1
            return priority[index % len(priority)]
        return self._random.choices(
            priority, weights=[state.config.weight for state in priority], k=1
        )[0]

    @staticmethod
    def _apply_snapshot(state: DeploymentRuntime[D], snapshot: RoutingStateSnapshot) -> None:
        state.distributed_active_requests = snapshot.active_requests
        if snapshot.last_latency_ms is not None:
            state.last_latency_ms = snapshot.last_latency_ms
