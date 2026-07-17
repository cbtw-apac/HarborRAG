from __future__ import annotations

import asyncio
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
from harborrag_adapters.models.common.routing_state_memory import InMemoryRoutingStateStore


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
