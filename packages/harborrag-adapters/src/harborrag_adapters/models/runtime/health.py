from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .distributed_config import ActiveHealthConfig
from .routing_state import RoutingStateStore
from .sync import AsyncLoopRunner


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    """Describe one active deployment probe result without response content."""

    healthy: bool
    latency_ms: float | None = None
    detail: str | None = None


class DeploymentHealthProbe(Protocol):
    """Probe one provider deployment through application-controlled health logic."""

    def check(self, logical_model: str, deployment: Any) -> HealthCheckResult:
        """Run one synchronous deployment probe."""

        ...

    async def acheck(self, logical_model: str, deployment: Any) -> HealthCheckResult:
        """Run one asynchronous deployment probe."""

        ...


class CallableHealthProbe:
    """Adapt sync and async callables into the active health probe protocol."""

    def __init__(
        self,
        check: Callable[[str, Any], HealthCheckResult],
        acheck: Callable[[str, Any], Awaitable[HealthCheckResult]] | None = None,
    ) -> None:
        """Store application-supplied probe functions."""

        self._check = check
        self._acheck = acheck

    def check(self, logical_model: str, deployment: Any) -> HealthCheckResult:
        """Run the synchronous probe callable."""

        return self._check(logical_model, deployment)

    async def acheck(self, logical_model: str, deployment: Any) -> HealthCheckResult:
        """Run an async probe or delegate the synchronous probe to a worker."""

        if self._acheck is not None:
            return await self._acheck(logical_model, deployment)
        return await asyncio.to_thread(self._check, logical_model, deployment)


_PROBE_IN_PROGRESS_DETAIL = "ProbeInProgress"


class ActiveHealthMonitor:
    """Run optional active probes and publish results to shared routing state."""

    def __init__(
        self,
        models: Mapping[str, Any],
        *,
        config: ActiveHealthConfig,
        store: RoutingStateStore,
        probe: DeploymentHealthProbe,
    ) -> None:
        """Bind model deployments, probe policy, and distributed health state."""

        self._models = models
        self.config = config
        self._store = store
        self._probe = probe
        self._task: asyncio.Task[None] | None = None
        self._runner: AsyncLoopRunner | None = None
        self._closed = False
        self._inflight_probes: dict[str, threading.Thread] = {}
        self._inflight_lock = threading.Lock()

    def check_once(self) -> tuple[tuple[str, HealthCheckResult], ...]:
        """Probe every enabled deployment synchronously and persist results."""

        results: list[tuple[str, HealthCheckResult]] = []
        for logical, deployment in self._deployments():
            started = time.perf_counter()
            result = self._check_with_timeout(logical, deployment)
            latency = result.latency_ms or (time.perf_counter() - started) * 1_000
            result = HealthCheckResult(result.healthy, latency, result.detail)
            key = deployment_state_key(logical, deployment.name)
            if result.detail != _PROBE_IN_PROGRESS_DETAIL:
                # A probe still running from a previous, overlapping call is
                # not a failure -- it may yet succeed. Persisting healthy=False
                # here would flap routing state for a deployment that is
                # merely slower than interval_seconds, not actually down.
                self._store.record_active_health(key, healthy=result.healthy, latency_ms=latency)
            results.append((key, result))
        return tuple(results)

    def _check_with_timeout(self, logical: str, deployment: Any) -> HealthCheckResult:
        """Run one sync probe bounded by ``config.timeout_seconds``.

        A hung probe (e.g. a health check that blocks on a stalled socket)
        must not stall ``check_once()`` forever, mirroring the timeout the
        async path already enforces via ``asyncio.wait_for``. Python cannot
        forcibly cancel a running thread, so the probe runs on a daemon
        thread: on timeout this returns promptly and reports the deployment
        unhealthy, while the stuck probe thread is abandoned in the
        background (it cannot block process exit, since it's daemonic).

        At most one abandoned thread accumulates per deployment: if a
        previous probe for this same deployment is still running when
        ``check_once()`` is called again, this reports unhealthy immediately
        instead of piling on another thread that will also be abandoned.
        """
        key = deployment_state_key(logical, deployment.name)
        with self._inflight_lock:
            previous = self._inflight_probes.get(key)
            if previous is not None and previous.is_alive():
                return HealthCheckResult(False, detail=_PROBE_IN_PROGRESS_DETAIL)

            outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

            def _target() -> None:
                try:
                    outcome.put((True, self._probe.check(logical, deployment)))
                except BaseException as exc:  # noqa: BLE001 - reported to the caller below
                    outcome.put((False, exc))

            thread = threading.Thread(target=_target, daemon=True, name="harbor-model-health-probe")
            self._inflight_probes[key] = thread
            thread.start()
        try:
            ok, value = outcome.get(timeout=self.config.timeout_seconds)
        except queue.Empty:
            return HealthCheckResult(False, detail="TimeoutError")
        if not ok:
            return HealthCheckResult(False, detail=type(value).__name__)
        result: HealthCheckResult = value
        return result

    async def acheck_once(self) -> tuple[tuple[str, HealthCheckResult], ...]:
        """Probe every enabled deployment concurrently and persist results."""

        async def probe(logical: str, deployment: Any) -> tuple[str, HealthCheckResult]:
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self._probe.acheck(logical, deployment),
                    timeout=self.config.timeout_seconds,
                )
            except Exception as exc:
                result = HealthCheckResult(False, detail=type(exc).__name__)
            latency = result.latency_ms or (time.perf_counter() - started) * 1_000
            normalized = HealthCheckResult(result.healthy, latency, result.detail)
            key = deployment_state_key(logical, deployment.name)
            await self._store.arecord_active_health(
                key, healthy=normalized.healthy, latency_ms=latency
            )
            return key, normalized

        return tuple(
            await asyncio.gather(
                *(probe(logical, deployment) for logical, deployment in self._deployments())
            )
        )

    def start(self) -> None:
        """Start health checks on one owned background event-loop thread."""

        if self._closed:
            raise RuntimeError("active health monitor is closed")
        if self._runner is None:
            self._runner = AsyncLoopRunner(thread_name="harbor-model-health")
            self._runner.run(self.astart())

    async def astart(self) -> None:
        """Start one background health task on the current event loop."""

        if self._closed:
            raise RuntimeError("active health monitor is closed")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="harbor-model-health")

    def close(self) -> None:
        """Stop the background monitor and its owned event-loop thread."""

        if self._runner is not None:
            self._runner.run(self.aclose())
            self._runner.stop()
            self._runner = None
            return
        if not self._closed:
            self._closed = True

    async def aclose(self) -> None:
        """Cancel the background task and wait for deterministic shutdown."""

        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            await self.acheck_once()
            await asyncio.sleep(self.config.interval_seconds)

    def _deployments(self) -> Sequence[tuple[str, Any]]:
        return tuple(
            (logical, deployment)
            for logical, model in self._models.items()
            for deployment in model.deployments
            if deployment.enabled
        )


def deployment_state_key(logical_model: str, deployment: str) -> str:
    """Build the stable distributed state key shared by routing and health probes."""

    return f"{logical_model}:{deployment}"
