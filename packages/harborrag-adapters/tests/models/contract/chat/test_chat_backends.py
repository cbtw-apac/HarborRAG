from __future__ import annotations

from typing import Any

import pytest
from chat_backend_contract import (
    BackendContractHarness,
    CallRecorder,
    FakeRouter,
    FakeSession,
    exercise_backend_contract,
)

from harborrag_adapters.models.chat.backend_config import (
    ChatBackendType,
    LiteLLMProxyConfig,
    ProxyAuthMode,
)
from harborrag_adapters.models.chat.backends.direct import LiteLLMDirectBackend
from harborrag_adapters.models.chat.backends.proxy import LiteLLMProxyBackend
from harborrag_adapters.models.chat.backends.router import LiteLLMRouterBackend
from harborrag_adapters.models.runtime.config import ConnectionPoolConfig
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership

pytestmark = pytest.mark.contract


def disabled_connections() -> SharedConnectionLifecycle:
    return SharedConnectionLifecycle(ConnectionPoolConfig(enabled=False))


@pytest.mark.asyncio
async def test_direct_sdk_backend_contract() -> None:
    recorder = CallRecorder()
    backend = LiteLLMDirectBackend(
        connections=disabled_connections(),
        completion=recorder.complete,
        acompletion=recorder.acomplete,
    )
    harness = BackendContractHarness(
        backend=backend,
        recorder=recorder,
        expected_type=ChatBackendType.DIRECT_SDK,
        expected_model="alias",
        assert_parameters=lambda value: None,
    )

    await exercise_backend_contract(harness)
    await backend.aclose()


@pytest.mark.asyncio
async def test_litellm_router_backend_contract() -> None:
    recorder = CallRecorder()
    router = FakeRouter(recorder)
    backend = LiteLLMRouterBackend(router, connections=disabled_connections())
    harness = BackendContractHarness(
        backend=backend,
        recorder=recorder,
        expected_type=ChatBackendType.LITELLM_ROUTER,
        expected_model="alias",
        assert_parameters=lambda value: None,
    )

    await exercise_backend_contract(harness)
    await backend.aclose()

    assert router.flushed == 1
    assert router.closed == 1


@pytest.mark.asyncio
async def test_litellm_proxy_backend_contract() -> None:
    recorder = CallRecorder()
    backend = LiteLLMProxyBackend(
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-secret",
            headers={"X-Team": "rag"},
        ),
        connections=disabled_connections(),
        completion=recorder.complete,
        acompletion=recorder.acomplete,
    )

    def assert_proxy(value: dict[str, Any]) -> None:
        assert value["api_base"] == "https://proxy.example.test"
        assert value["api_key"] == "proxy-secret"
        assert value["extra_headers"] == {"X-Team": "rag"}
        assert "custom_llm_provider" not in value

    harness = BackendContractHarness(
        backend=backend,
        recorder=recorder,
        expected_type=ChatBackendType.LITELLM_PROXY,
        expected_model="litellm_proxy/alias",
        assert_parameters=assert_proxy,
    )

    await exercise_backend_contract(harness)
    await backend.aclose()


@pytest.mark.asyncio
async def test_shared_connection_lifecycle_reuses_and_closes_one_session() -> None:
    recorder = CallRecorder()
    session = FakeSession()
    lifecycle = SharedConnectionLifecycle(
        ConnectionPoolConfig(enabled=True),
        session_factory=lambda config: session,
    )
    backend = LiteLLMDirectBackend(
        connections=lifecycle,
        connection_ownership=ResourceOwnership.OWNED,
        completion=recorder.complete,
        acompletion=recorder.acomplete,
    )

    first = await backend.acomplete(model="one")
    second = await backend.acomplete(model="two")
    await backend.aclose()

    assert first["shared_session"] is session
    assert second["shared_session"] is session
    assert session.closed == 1


def test_proxy_backend_merges_request_headers_and_avoids_double_prefix() -> None:
    recorder = CallRecorder()
    backend = LiteLLMProxyBackend(
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-secret",
            headers={"X-Team": "rag", "X-Shared": "configured"},
        ),
        connections=disabled_connections(),
        completion=recorder.complete,
        acompletion=recorder.acomplete,
    )

    result = backend.complete(
        model="litellm_proxy/alias",
        api_key="provider-secret",
        custom_llm_provider="openai",
        extra_headers={"X-Shared": "request", "X-Request": "one"},
    )

    assert result["model"] == "litellm_proxy/alias"
    assert result["api_key"] == "proxy-secret"
    assert result["extra_headers"] == {
        "X-Team": "rag",
        "X-Shared": "request",
        "X-Request": "one",
    }
    assert "custom_llm_provider" not in result


def test_proxy_backend_rejects_request_auth_header_overrides() -> None:
    backend = LiteLLMProxyBackend(
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-secret",
            auth_mode=ProxyAuthMode.X_LITELLM_API_KEY,
        ),
        connections=disabled_connections(),
        completion=lambda **kwargs: kwargs,
        acompletion=lambda **kwargs: kwargs,
    )

    for header in ("Authorization", "X-LiteLLM-Api-Key", "x-api-key"):
        with pytest.raises(ValueError, match="cannot override"):
            backend.prepare_parameters(
                {
                    "model": "alias",
                    "extra_headers": {header: "attacker-controlled"},
                }
            )


def test_proxy_backend_installs_selected_auth_after_configured_headers() -> None:
    backend = LiteLLMProxyBackend(
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-secret",
            auth_mode=ProxyAuthMode.X_LITELLM_API_KEY,
            headers={
                "Authorization": "stale-bearer",
                "X-LiteLLM-Api-Key": "stale-proxy-key",
            },
        ),
        connections=disabled_connections(),
        completion=lambda **kwargs: kwargs,
        acompletion=lambda **kwargs: kwargs,
    )

    result = backend.prepare_parameters({"model": "alias"})

    assert result["extra_headers"] == {"x-litellm-api-key": "proxy-secret"}
