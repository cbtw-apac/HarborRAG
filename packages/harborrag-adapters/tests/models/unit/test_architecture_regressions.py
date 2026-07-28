from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.errors import normalize_exception
from harborrag_adapters.models.chat.registry import (
    HarborProvider,
    ProviderRegistry,
)
from harborrag_adapters.models.chat.validation import validate_chat_configuration
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.embed.validation import validate_embed_configuration
from harborrag_adapters.models.rerank import HarborRerankClientConfig
from harborrag_adapters.models.rerank.validation import validate_rerank_configuration
from harborrag_adapters.models.runtime.config import (
    CircuitBreakerConfig,
    RoutingEngine,
    RoutingStrategy,
    TelemetryFailureMode,
)
from harborrag_adapters.models.runtime.health import deployment_state_key
from harborrag_adapters.models.runtime.lifecycle import close_async_callbacks
from harborrag_adapters.models.runtime.litellm_backend import litellm_routing_strategy
from harborrag_adapters.models.runtime.provider import ProviderMetadata
from harborrag_adapters.models.runtime.routing import (
    DeploymentSelector,
    NoHealthyDeploymentError,
)
from harborrag_adapters.models.runtime.routing_state import RoutingAdmissionError
from harborrag_adapters.models.runtime.routing_state_memory import (
    InMemoryRoutingStateStore,
)
from harborrag_adapters.models.runtime.security import PrivacyConfig, PrivacySanitizer
from harborrag_adapters.models.runtime.telemetry import TelemetryDispatcher
from harborrag_core.models.errors import (
    HarborChatError,
    HarborChatProviderError,
    HarborChatTimeoutError,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_registry_detects_duplicates_and_reports_unknown_provider() -> None:
    descriptor = ProviderMetadata(HarborProvider.OPENAI, "openai")

    with pytest.raises(ValueError, match="duplicate provider registration"):
        ProviderRegistry([descriptor, descriptor])

    registry = ProviderRegistry([descriptor])
    assert registry.get(HarborProvider.OPENAI) is descriptor
    assert tuple(registry.all()) == (HarborProvider.OPENAI,)
    with pytest.raises(KeyError, match="supported providers: openai"):
        registry.get(HarborProvider.ANTHROPIC)
    assert ProviderRegistry.default() is not ProviderRegistry.default()


@dataclass(frozen=True)
class RoutingDeployment:
    name: str
    enabled: bool = True
    weight: float = 1.0
    order: int = 0
    max_parallel_requests: int | None = None


@pytest.mark.asyncio
async def test_shared_routing_selection_health_and_leases() -> None:
    deployments = (
        RoutingDeployment("a", max_parallel_requests=1),
        RoutingDeployment("b"),
    )
    circuit = CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=10)
    round_robin = DeploymentSelector(
        {"model": deployments},
        strategy=RoutingStrategy.ROUND_ROBIN,
        circuit_breaker=circuit,
        enable_health_tracking=True,
    )

    first = await round_robin.select("model", deployments)
    second = await round_robin.select("model", deployments)
    assert (first.config.name, second.config.name) == ("a", "b")
    async with round_robin.lease(first):
        assert first.active_requests == 1
    assert first.active_requests == 0

    latency = DeploymentSelector(
        {"model": deployments},
        strategy=RoutingStrategy.LATENCY,
        circuit_breaker=circuit,
        enable_health_tracking=True,
    )
    state_a = await latency.select("model", deployments, exclude={"b"})
    state_b = await latency.select("model", deployments, exclude={"a"})
    await latency.record_success(state_a, 20)
    await latency.record_success(state_b, 5)
    assert (await latency.select("model", deployments)).config.name == "b"

    await latency.record_failure(state_b, retryable=True)
    assert (await latency.select("model", deployments)).config.name == "a"
    with pytest.raises(NoHealthyDeploymentError):
        await latency.select("model", deployments, exclude={"a"})
    await latency.record_success(state_b, 4)
    assert (await latency.select("model", deployments)).config.name == "b"


@dataclass(frozen=True)
class RateLimitedDeployment:
    name: str = "a"
    enabled: bool = True
    weight: float = 1.0
    order: int = 0
    max_parallel_requests: int | None = None
    rpm: int | None = 1
    tpm: int | None = None


def test_admission_rejection_preserves_active_request_accounting() -> None:
    deployment = RateLimitedDeployment()
    selector = DeploymentSelector(
        {"model": (deployment,)},
        strategy=RoutingStrategy.ORDERED,
        circuit_breaker=CircuitBreakerConfig(),
        enable_health_tracking=True,
    )
    state = selector.select_sync("model", (deployment,))
    with selector.lease_sync(state, logical_model="model"):
        assert state.active_requests == 1
        with pytest.raises(RoutingAdmissionError, match="rpm"):
            with selector.lease_sync(state, logical_model="model"):
                pass
        assert state.active_requests == 1
    assert state.active_requests == 0


@pytest.mark.asyncio
async def test_disabled_circuit_breaker_never_records_distributed_failures() -> None:
    store = InMemoryRoutingStateStore()
    deployments = (RoutingDeployment("a"),)
    selector = DeploymentSelector(
        {"model": deployments},
        strategy=RoutingStrategy.ORDERED,
        circuit_breaker=CircuitBreakerConfig(enabled=False, failure_threshold=1),
        enable_health_tracking=True,
        state_store=store,
    )
    state = await selector.select("model", deployments)
    await selector.record_failure(state, retryable=True)
    snapshot = store.snapshot(deployment_state_key("model", "a"))
    assert snapshot.consecutive_failures == 0
    assert snapshot.circuit_open_until == 0.0
    assert (await selector.select("model", deployments)).config.name == "a"


def test_async_concurrency_semaphores_are_bound_per_event_loop() -> None:
    deployments = (RoutingDeployment("a", max_parallel_requests=1),)
    selector = DeploymentSelector(
        {"model": deployments},
        strategy=RoutingStrategy.ORDERED,
        circuit_breaker=CircuitBreakerConfig(),
        enable_health_tracking=True,
    )

    async def use_once() -> None:
        state = await selector.select("model", deployments)
        async with selector.lease(state):
            assert state.active_requests == 1

    asyncio.run(use_once())
    asyncio.run(use_once())


@pytest.mark.asyncio
async def test_lifecycle_closes_every_callback_before_raising() -> None:
    events: list[str] = []

    async def first() -> None:
        events.append("first")
        raise RuntimeError("first failed")

    async def second() -> None:
        events.append("second")

    with pytest.raises(RuntimeError, match="first failed"):
        await close_async_callbacks([first, second])
    assert events == ["first", "second"]

    async def third() -> None:
        raise ValueError("third failed")

    with pytest.raises(ExceptionGroup) as captured:
        await close_async_callbacks([first, third])
    assert len(captured.value.exceptions) == 2


def test_error_mapping_enriches_existing_errors_and_redacts_credentials() -> None:
    existing = HarborChatProviderError("failed", retryable=True)
    enriched = normalize_exception(
        existing,
        provider="openai",
        logical_model="primary",
        provider_model="gpt",
        deployment="a",
        request_id="request-1",
    )
    assert enriched is existing
    assert enriched.operation == "chat"
    assert enriched.request_id == "request-1"

    timeout = normalize_exception(TimeoutError("slow"))
    assert isinstance(timeout, HarborChatTimeoutError)
    assert timeout.retryable is True

    redacted = normalize_exception(RuntimeError("Authorization: Bearer top-secret"))
    assert "top-secret" not in str(redacted)


def test_error_mapping_preserves_provider_request_id() -> None:
    class Response:
        headers = {"x-request-id": "provider-123"}

    class ProviderFailure(Exception):
        status_code = 503
        llm_provider = "provider-x"
        response = Response()

    error: HarborChatError = normalize_exception(ProviderFailure("temporarily unavailable"))
    assert error.provider_request_id == "provider-123"
    assert error.retryable is True
    assert error.to_dict()["provider_request_id"] == "provider-123"


def test_identifier_privacy_and_litellm_strategy_translation() -> None:
    sanitizer = PrivacySanitizer(PrivacyConfig(hash_user_identifiers=True))
    assert sanitizer.identifier("user-1") == hashlib.sha256(b"user-1").hexdigest()
    assert sanitizer.identifier(None) is None
    raw = PrivacySanitizer(PrivacyConfig(hash_user_identifiers=False))
    assert raw.identifier("user-1") == "user-1"
    assert litellm_routing_strategy(RoutingStrategy.LEAST_BUSY) == "least-busy"
    with pytest.raises(ValueError, match="round_robin"):
        litellm_routing_strategy(RoutingStrategy.ROUND_ROBIN)


@dataclass
class ClosingHook:
    events: list[str]
    name: str
    fails: bool = False

    async def aclose(self) -> None:
        self.events.append(self.name)
        if self.fails:
            raise RuntimeError(self.name)


@pytest.mark.asyncio
async def test_telemetry_close_is_complete_in_raise_and_ignore_modes() -> None:
    events: list[str] = []
    hooks = [ClosingHook(events, "first", True), ClosingHook(events, "second")]
    raising = TelemetryDispatcher(
        hooks,
        failure_mode=TelemetryFailureMode.RAISE,
        logger=logging.getLogger(__name__),
    )
    with pytest.raises(RuntimeError, match="first"):
        await raising.aclose()
    assert events == ["first", "second"]

    events.clear()
    ignoring = TelemetryDispatcher(
        hooks,
        failure_mode=TelemetryFailureMode.IGNORE,
        logger=logging.getLogger(__name__),
    )
    await ignoring.aclose()
    assert events == ["first", "second"]


def test_example_yaml_loads_all_model_families(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("COHERE_API_KEY", "cohere-secret")
    path = Path(__file__).resolve().parents[5] / "config" / "models.example.yaml"

    chat = HarborChatClientConfig.from_file(path)
    embed = HarborEmbedClientConfig.from_file(path)
    rerank = HarborRerankClientConfig.from_file(path)

    assert chat.default_model == "primary"
    assert embed.models["primary"].embedding_space == "harbor-production-v1"
    assert rerank.default_model == "primary"
    assert "openai-secret" not in repr(chat)


def test_advanced_example_yaml_loads_base_and_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "OPENAI_API_KEY": "openai-primary",
        "OPENAI_API_KEY_SECONDARY": "openai-secondary",
        "ANTHROPIC_API_KEY": "anthropic",
        "AWS_REGION": "us-east-1",
        "COHERE_API_KEY": "cohere-primary",
        "COHERE_API_KEY_SECONDARY": "cohere-secondary",
        "AZURE_OPENAI_API_KEY": "azure",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
        "AZURE_OPENAI_API_VERSION": "2025-01-01-preview",
        "AZURE_OPENAI_CHAT_DEPLOYMENT": "chat-production",
        "AZURE_OPENAI_EMBED_DEPLOYMENT": "embed-production",
        "GEMINI_API_KEY": "gemini",
        "OPENAI_COMPATIBLE_API_KEY": "gateway",
        "OPENAI_COMPATIBLE_API_BASE": "https://gateway.example.com/v1",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    path = Path(__file__).resolve().parents[5] / "config" / "models.advance.example.yaml"
    source = path.read_text(encoding="utf-8")
    assert "api_key: ${AZURE_OPENAI_API_KEY}" in source

    configs = (
        HarborChatClientConfig,
        HarborEmbedClientConfig,
        HarborRerankClientConfig,
    )
    base = tuple(config.from_file(path) for config in configs)
    production = tuple(config.from_file(path, profile="production") for config in configs)
    for chat, embed, rerank in (base, production):
        validate_chat_configuration(chat)
        validate_embed_configuration(embed)
        validate_rerank_configuration(rerank)

    assert all(not config.cache.enabled for config in base)
    assert base[0].routing.engine is RoutingEngine.LITELLM_ROUTER
    assert base[1].routing.engine is RoutingEngine.HARBOR
    assert base[2].routing.engine is RoutingEngine.HARBOR
    assert tuple(config.timeouts.request_seconds for config in production) == (90, 90, 90)
