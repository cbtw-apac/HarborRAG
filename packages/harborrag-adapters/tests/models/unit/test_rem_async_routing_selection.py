"""Async routing selection reads distributed state without a blocking call on the loop."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_adapters.models.runtime.config import (
    CircuitBreakerConfig,
    RetryPolicyConfig,
    RoutingStrategy,
)
from harborrag_adapters.models.runtime.routing import DeploymentSelector
from harborrag_adapters.models.runtime.routing_cursor import RoutingExecutionCursor
from harborrag_adapters.models.runtime.routing_state import RoutingStateSnapshot
from harborrag_adapters.models.runtime.routing_state_memory import InMemoryRoutingStateStore

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@dataclass
class Deployment:
    name: str
    enabled: bool = True
    weight: float = 1.0
    order: int = 0
    max_parallel_requests: int | None = None
    rpm: int | None = None
    tpm: int | None = None


@dataclass
class LogicalModel:
    deployments: tuple[Deployment, ...]
    aliases: frozenset[str] = frozenset()
    fallbacks: tuple[str, ...] = ()


class _AsyncReadsOnlyStore(InMemoryRoutingStateStore):
    """Routing state whose blocking read fails, as a Redis read would block the loop."""

    def __init__(self) -> None:
        super().__init__()
        self.async_reads: list[str] = []

    def snapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        raise AssertionError("blocking routing-state read on the event loop")

    async def asnapshot(self, deployment_key: str) -> RoutingStateSnapshot:
        self.async_reads.append(deployment_key)
        return super().snapshot(deployment_key)


def _route(
    models: dict[str, LogicalModel], start: str
) -> tuple[
    RoutingExecutionCursor[Deployment], DeploymentSelector[Deployment], _AsyncReadsOnlyStore
]:
    store = _AsyncReadsOnlyStore()
    selector = DeploymentSelector(
        {name: model.deployments for name, model in models.items()},
        strategy=RoutingStrategy.ORDERED,
        circuit_breaker=CircuitBreakerConfig(),
        enable_health_tracking=True,
        state_store=store,
    )
    return RoutingExecutionCursor(models, start, RetryPolicyConfig()), selector, store


@pytest.mark.asyncio
async def test_async_next_attempt_reads_routing_state_asynchronously() -> None:
    cursor, selector, store = _route(
        {"demo-chat": LogicalModel((Deployment("primary"), Deployment("secondary", order=1)))},
        "demo-chat",
    )

    attempt = await cursor.next_attempt(selector)

    assert attempt is not None
    assert attempt.public.deployment.name == "primary"
    assert len(store.async_reads) == 2


@pytest.mark.asyncio
async def test_async_next_attempt_falls_back_to_the_next_model() -> None:
    cursor, selector, _store = _route(
        {
            "demo-chat": LogicalModel(
                (Deployment("primary", enabled=False),), fallbacks=("demo-backup",)
            ),
            "demo-backup": LogicalModel((Deployment("backup"),)),
        },
        "demo-chat",
    )

    attempt = await cursor.next_attempt(selector)

    assert attempt is not None
    assert attempt.logical_model == "demo-backup"
    assert cursor.model_fallback_count == 1
