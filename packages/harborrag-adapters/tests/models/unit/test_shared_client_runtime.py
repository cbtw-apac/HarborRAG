"""Tests for the runtime resolution chat, embedding, and reranking clients share.

`ModelClientRuntimeMixin` resolves telemetry, the runtime-services bundle, their
ownership, and the optional active health monitor for all three families. These
tests pin the behavior per family, because the ownership defaults are declared in
two places: `ModelClientDependencies` for embedding/reranking and
`ChatClientDependencies` for chat.
"""

from __future__ import annotations

from typing import Any

import pytest
from model_runtime_support import (
    chat_config,
    embed_client,
    embed_config,
    rerank_client,
    rerank_config,
)

from harborrag_adapters.models.chat import ChatClientDependencies, HarborChatClient
from harborrag_adapters.models.runtime.budget import NoopBudgetPolicy
from harborrag_adapters.models.runtime.config import ModelClientConfig
from harborrag_adapters.models.runtime.distributed_config import ActiveHealthConfig
from harborrag_adapters.models.runtime.health import HealthCheckResult
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_adapters.models.runtime.routing_state_memory import InMemoryRoutingStateStore
from harborrag_adapters.models.runtime.runtime_services import ModelRuntimeServices
from harborrag_adapters.models.runtime.singleflight import NoopSingleFlight

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class CountingRoutingStateStore(InMemoryRoutingStateStore):
    """Record how often a routing-state store is closed."""

    def __init__(self) -> None:
        """Start the memory store with a zeroed close counter."""

        super().__init__()
        self.close_count = 0

    def close(self) -> None:
        """Count and perform synchronous teardown."""

        self.close_count += 1
        super().close()


class CountingRuntimeServices(ModelRuntimeServices):
    """Record how often an injected services bundle is closed."""

    def __init__(self) -> None:
        """Build a memory-backed bundle that counts its own teardown."""

        super().__init__(
            cache=None,
            routing_state=InMemoryRoutingStateStore(),
            singleflight=NoopSingleFlight(),
            budget=NoopBudgetPolicy(),
        )
        self.close_count = 0
        self.aclose_count = 0

    def close(self) -> None:
        """Count and perform synchronous teardown."""

        self.close_count += 1
        super().close()

    async def aclose(self) -> None:
        """Count and perform asynchronous teardown."""

        self.aclose_count += 1
        await super().aclose()


class RecordingHealthProbe:
    """Report every deployment healthy and record that it was probed."""

    def __init__(self) -> None:
        """Start with no recorded probes."""

        self.checked: list[str] = []

    def check(self, logical_model: str, deployment: Any) -> HealthCheckResult:
        """Record one synchronous probe and report the deployment healthy."""

        self.checked.append(logical_model)
        return HealthCheckResult(healthy=True, latency_ms=1.0)

    async def acheck(self, logical_model: str, deployment: Any) -> HealthCheckResult:
        """Record one asynchronous probe and report the deployment healthy."""

        return self.check(logical_model, deployment)


def _chat_client(config: Any, **dependencies: Any) -> HarborChatClient:
    """Build a chat client with the same call shape as the other families."""

    return HarborChatClient(config, ChatClientDependencies(**dependencies))


def _auto_start_health[ConfigT: ModelClientConfig](config: ConfigT) -> ConfigT:
    """Return the config with active health enabled and started automatically."""

    routing = config.routing.model_copy(
        update={
            "active_health": ActiveHealthConfig(
                enabled=True,
                start_automatically=True,
                interval_seconds=3_600.0,
            )
        }
    )
    return config.model_copy(update={"routing": routing})


FAMILIES = [
    (_chat_client, chat_config),
    (embed_client, embed_config),
    (rerank_client, rerank_config),
]
FAMILY_IDS = ["chat", "embed", "rerank"]


@pytest.mark.parametrize(("build", "config_factory"), FAMILIES, ids=FAMILY_IDS)
def test_injected_runtime_services_are_borrowed_by_default(build, config_factory) -> None:
    services = CountingRuntimeServices()

    client = build(config_factory(), runtime_services=services)
    client.close()

    assert services.close_count == 0


@pytest.mark.parametrize(("build", "config_factory"), FAMILIES, ids=FAMILY_IDS)
def test_injected_runtime_services_close_when_explicitly_owned(build, config_factory) -> None:
    services = CountingRuntimeServices()

    client = build(
        config_factory(),
        runtime_services=services,
        services_ownership=ResourceOwnership.OWNED,
    )
    client.close()

    assert services.close_count == 1


def test_client_built_runtime_services_are_still_owned() -> None:
    routing_state = CountingRoutingStateStore()

    client = embed_client(embed_config(), routing_state=routing_state)
    client.close()

    assert routing_state.close_count == 1


@pytest.mark.parametrize(("build", "config_factory"), FAMILIES, ids=FAMILY_IDS)
def test_automatic_health_start_requires_an_injected_probe(build, config_factory) -> None:
    config = _auto_start_health(config_factory())

    with pytest.raises(ValueError, match="requires an injected health probe"):
        build(config)


@pytest.mark.parametrize(("build", "config_factory"), FAMILIES, ids=FAMILY_IDS)
def test_automatic_health_start_launches_the_monitor(build, config_factory) -> None:
    config = _auto_start_health(config_factory())
    probe = RecordingHealthProbe()

    client = build(config, health_probe=probe)
    try:
        assert client._health_monitor is not None
        assert client.check_deployment_health()
    finally:
        client.close()

    assert probe.checked
