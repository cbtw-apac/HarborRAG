from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from harborrag_adapters.models.common.distributed_config import ActiveHealthConfig
from harborrag_adapters.models.common.health import (
    ActiveHealthMonitor,
    CallableHealthProbe,
    HealthCheckResult,
    deployment_state_key,
)
from harborrag_adapters.models.common.routing_state_memory import (
    InMemoryRoutingStateStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_callable_health_probe_and_monitor_sync() -> None:
    config = SimpleNamespace(
        deployments=(
            SimpleNamespace(name="a", enabled=True),
            SimpleNamespace(name="b", enabled=False),
        )
    )
    store = InMemoryRoutingStateStore(clock=lambda: 10.0)
    probe = CallableHealthProbe(lambda _logical, _deployment: HealthCheckResult(True, 3.0, "ok"))
    assert probe.check("primary", config.deployments[0]).healthy
    monitor = ActiveHealthMonitor(
        {"primary": config},
        config=ActiveHealthConfig(enabled=True, interval_seconds=0.01, timeout_seconds=0.1),
        store=store,
        probe=probe,
    )
    results = monitor.check_once()
    assert results[0][0] == deployment_state_key("primary", "a")
    assert store.snapshot("primary:a").active_healthy is True
    monitor.close()


def test_check_once_times_out_on_a_hung_sync_probe_instead_of_blocking_forever() -> None:
    config = SimpleNamespace(
        deployments=(SimpleNamespace(name="a", enabled=True),),
    )
    store = InMemoryRoutingStateStore(clock=lambda: 10.0)
    release = threading.Event()

    def hung_check(_logical: str, _deployment: Any) -> HealthCheckResult:
        # Simulate a probe blocked on a stalled socket -- it never returns on
        # its own within the test, so a correct fix must not wait for it.
        release.wait(timeout=5.0)
        return HealthCheckResult(True)

    probe = CallableHealthProbe(hung_check)
    monitor = ActiveHealthMonitor(
        {"primary": config},
        config=ActiveHealthConfig(enabled=True, interval_seconds=0.01, timeout_seconds=0.05),
        store=store,
        probe=probe,
    )
    started = time.perf_counter()
    results = monitor.check_once()
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, "check_once() must return promptly on a hung probe, not block on it"
    assert results[0][1].healthy is False
    assert results[0][1].detail == "TimeoutError"
    assert store.snapshot("primary:a").active_healthy is False
    release.set()
    monitor.close()


def test_check_once_does_not_pile_up_abandoned_probe_threads() -> None:
    """Repeated check_once() calls against a permanently hung deployment must
    not spawn a new abandoned thread every time -- at most one in-flight
    probe thread per deployment should ever exist."""
    config = SimpleNamespace(
        deployments=(SimpleNamespace(name="a", enabled=True),),
    )
    store = InMemoryRoutingStateStore(clock=lambda: 10.0)
    release = threading.Event()

    def hung_check(_logical: str, _deployment: Any) -> HealthCheckResult:
        release.wait(timeout=5.0)
        return HealthCheckResult(True)

    probe = CallableHealthProbe(hung_check)
    monitor = ActiveHealthMonitor(
        {"primary": config},
        config=ActiveHealthConfig(enabled=True, interval_seconds=0.01, timeout_seconds=0.02),
        store=store,
        probe=probe,
    )
    try:
        for _ in range(10):
            results = monitor.check_once()
            assert results[0][1].healthy is False
        probe_threads = [
            t for t in threading.enumerate() if t.name == "harbor-model-health-probe"
        ]
        assert len(probe_threads) == 1
    finally:
        release.set()
        monitor.close()


def test_check_once_does_not_persist_unhealthy_for_probe_in_progress() -> None:
    """A probe still running from an overlapping call is not a failure -- it
    may yet succeed. Persisting healthy=False here would flap routing state
    for a deployment that is merely slower than interval_seconds, not
    actually down, so the ProbeInProgress result must not be persisted."""
    config = SimpleNamespace(
        deployments=(SimpleNamespace(name="a", enabled=True),),
    )
    store = InMemoryRoutingStateStore(clock=lambda: 10.0)
    store.record_active_health("primary:a", healthy=True, latency_ms=5.0)

    probe = CallableHealthProbe(lambda _logical, _deployment: HealthCheckResult(True))
    monitor = ActiveHealthMonitor(
        {"primary": config},
        config=ActiveHealthConfig(enabled=True, interval_seconds=0.01, timeout_seconds=0.05),
        store=store,
        probe=probe,
    )
    # Simulate an overlapping in-flight probe for this deployment directly,
    # without waiting through a real timeout first (which would itself be a
    # legitimate failure, muddying what this test isolates).
    stuck = threading.Event()
    still_running = threading.Thread(target=stuck.wait, daemon=True)
    still_running.start()
    monitor._inflight_probes["primary:a"] = still_running

    try:
        results = monitor.check_once()
        assert results[0][1].detail == "ProbeInProgress"
        assert results[0][1].healthy is False
        # The pre-existing healthy=True state must survive untouched.
        assert store.snapshot("primary:a").active_healthy is True
    finally:
        stuck.set()
        monitor.close()


@pytest.mark.asyncio
async def test_active_health_async_success_failure_and_background() -> None:
    models = {
        "primary": SimpleNamespace(
            deployments=(
                SimpleNamespace(name="a", enabled=True),
                SimpleNamespace(name="b", enabled=True),
            )
        )
    }
    store = InMemoryRoutingStateStore()

    async def check(_logical: str, deployment: Any) -> HealthCheckResult:
        if deployment.name == "b":
            raise TimeoutError("down")
        return HealthCheckResult(True, 1.0)

    probe = CallableHealthProbe(lambda _l, _d: HealthCheckResult(True), check)
    assert (await probe.acheck("primary", models["primary"].deployments[0])).healthy
    monitor = ActiveHealthMonitor(
        models,
        config=ActiveHealthConfig(enabled=True, interval_seconds=0.01, timeout_seconds=0.1),
        store=store,
        probe=probe,
    )
    results = await monitor.acheck_once()
    assert {result.healthy for _, result in results} == {True, False}
    await monitor.astart()
    await asyncio.sleep(0.025)
    await monitor.aclose()
    await monitor.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await monitor.astart()
