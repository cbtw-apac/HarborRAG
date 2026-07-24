from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass

import pytest

from harborrag_adapters.models.runtime.config import (
    CircuitBreakerConfig,
    RetryPolicyConfig,
    RoutingStrategy,
)
from harborrag_adapters.models.runtime.lifecycle import (
    AsyncLifecycleResource,
    LifecycleResource,
    ResourceOwnership,
    close_async_callbacks,
    close_async_resources,
    close_callbacks,
    close_resources,
)
from harborrag_adapters.models.runtime.retry import RetryController
from harborrag_adapters.models.runtime.routing import DeploymentSelector
from harborrag_adapters.models.runtime.sync import (
    AsyncLoopRunner,
    run_awaitable_synchronously,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@dataclass
class Deployment:
    """Provide the routing attributes required by the selector."""

    name: str
    enabled: bool = True
    weight: float = 1.0
    order: int = 0
    max_parallel_requests: int | None = None


def test_lifecycle_closes_all_owned_resources_and_aggregates() -> None:
    """Close every owned resource even when an earlier close fails."""
    calls: list[str] = []

    def good() -> None:
        calls.append("good")

    def bad() -> None:
        calls.append("bad")
        raise RuntimeError("bad")

    with pytest.raises(RuntimeError, match="bad"):
        close_callbacks((bad, good))
    assert calls == ["bad", "good"]
    calls.clear()
    with pytest.raises(ExceptionGroup):
        close_callbacks((bad, bad))
    close_resources(
        (
            LifecycleResource(good, ResourceOwnership.OWNED),
            LifecycleResource(bad, ResourceOwnership.BORROWED),
        )
    )
    assert calls[-1] == "good"


@pytest.mark.asyncio
async def test_async_lifecycle_closes_all_resources() -> None:
    """Apply aggregate cleanup semantics to asynchronous resources."""
    calls: list[str] = []

    async def good() -> None:
        calls.append("good")

    async def bad() -> None:
        calls.append("bad")
        raise RuntimeError("bad")

    with pytest.raises(RuntimeError, match="bad"):
        await close_async_callbacks((bad, good))
    assert calls == ["bad", "good"]
    calls.clear()
    await close_async_resources(
        (
            AsyncLifecycleResource(good, ResourceOwnership.OWNED),
            AsyncLifecycleResource(bad, ResourceOwnership.BORROWED),
        )
    )
    assert calls == ["good"]


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (RoutingStrategy.ORDERED, "a"),
        (RoutingStrategy.ROUND_ROBIN, "a"),
        (RoutingStrategy.LEAST_BUSY, "a"),
        (RoutingStrategy.LATENCY, "a"),
    ],
)
def test_deployment_selector_strategies(strategy: RoutingStrategy, expected: str) -> None:
    """Select deployments according to each deterministic strategy."""
    items = (Deployment("b", order=2), Deployment("a", order=1))
    selector = DeploymentSelector(
        {"primary": items},
        strategy=strategy,
        circuit_breaker=CircuitBreakerConfig(failure_threshold=1),
        enable_health_tracking=True,
        random_source=random.Random(0),
    )
    selected = selector.select_sync("primary", items)
    assert selected.config.name == expected
    selector.record_success_sync(selected, 12)
    assert selected.last_latency_ms == 12
    selector.record_failure_sync(selected, retryable=True)
    assert not selected.available(time.monotonic())


def test_weighted_selection_and_concurrency_leases() -> None:
    """Exercise weighted selection and synchronous deployment leases."""
    items = (Deployment("a", weight=0.1), Deployment("b", weight=10))
    selector = DeploymentSelector(
        {"primary": items},
        strategy=RoutingStrategy.WEIGHTED,
        circuit_breaker=CircuitBreakerConfig(),
        enable_health_tracking=False,
        random_source=random.Random(0),
    )
    selected = selector.select_sync("primary", items)
    assert selected.config.name in {"a", "b"}
    with selector.lease_sync(selected):
        assert selected.active_requests == 1
    assert selected.active_requests == 0


@pytest.mark.asyncio
async def test_async_semaphore_cancellation_does_not_consume_permit() -> None:
    """Ensure a cancelled waiter cannot leak an async concurrency permit."""
    deployment = Deployment("only", max_parallel_requests=1)
    selector = DeploymentSelector(
        {"primary": (deployment,)},
        strategy=RoutingStrategy.ORDERED,
        circuit_breaker=CircuitBreakerConfig(),
        enable_health_tracking=True,
    )
    state = selector.select_sync("primary", (deployment,))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with selector.lease(state):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with selector.lease(state):
            return

    holder_task = asyncio.create_task(holder())
    await entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    release.set()
    await holder_task
    async with selector.lease(state):
        assert state.active_requests == 1
    assert state.active_requests == 0


def test_retry_controller_delays_and_sync_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound exponential delays and delegate synchronous sleeping."""
    controller = RetryController(
        RetryPolicyConfig(base_delay_seconds=1, max_delay_seconds=2, jitter_ratio=0)
    )
    assert controller.delay_seconds(1) == 1
    assert controller.delay_seconds(3) == 2
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", calls.append)
    controller.sleep_sync(1)
    assert calls == [1]


@pytest.mark.asyncio
async def test_retry_controller_async_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delegate asynchronous sleeping with the computed delay."""
    calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    controller = RetryController(
        RetryPolicyConfig(base_delay_seconds=1, max_delay_seconds=1, jitter_ratio=0)
    )
    await controller.sleep(1)
    assert calls == [1]


def test_async_loop_runner_and_sync_awaitable() -> None:
    """Reuse one event loop and reject work after shutdown."""
    runner = AsyncLoopRunner(thread_name="test-loop")

    async def value() -> int:
        return 7

    assert runner.run(value()) == 7
    assert runner.submit(value()).result() == 7
    runner.stop()
    runner.stop()
    coroutine = value()
    with pytest.raises(RuntimeError, match="closed"):
        runner.run(coroutine)
    coroutine.close()
    assert run_awaitable_synchronously(value(), thread_name="direct") == 7


@pytest.mark.asyncio
async def test_sync_awaitable_inside_running_loop() -> None:
    """Run a coroutine safely from a thread that already owns an event loop."""

    async def value() -> int:
        return threading.get_ident()

    assert isinstance(run_awaitable_synchronously(value(), thread_name="nested"), int)
