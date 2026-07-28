from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.models.chat import (
    ChatBackendType,
    HarborChatClient,
    HarborChatClientConfig,
)
from harborrag_adapters.models.chat.backend_config import (
    ChatBackendConfig,
    LiteLLMProxyConfig,
    ProxyAuthMode,
    ProxyMetadataConfig,
)
from harborrag_adapters.models.chat.backends import (
    LiteLLMDirectBackend,
    LiteLLMProxyBackend,
    LiteLLMRouterBackend,
)
from harborrag_adapters.models.chat.backends.factory import build_chat_backend
from harborrag_adapters.models.chat.configs import (
    HarborChatModelConfig,
    HarborChatProviderConfig,
)
from harborrag_adapters.models.chat.registry import HarborProvider, ProviderRegistry
from harborrag_adapters.models.runtime.config import (
    ConnectionPoolConfig,
    ObservabilityConfig,
    RoutingConfig,
    RoutingEngine,
    RoutingStrategy,
)
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.errors import HarborChatConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def deployment(
    provider: HarborProvider, *, model: str = "openai/model"
) -> HarborChatProviderConfig:
    return HarborChatProviderConfig(
        name="primary-a",
        provider=provider,
        model=model,
        api_key="secret" if provider is not HarborProvider.LITELLM_PROXY else None,
        capabilities=HarborChatCapabilities(streaming=True),
    )


def config_for(
    backend: ChatBackendConfig,
    deployment_config: HarborChatProviderConfig,
    *,
    routing: RoutingConfig | None = None,
) -> HarborChatClientConfig:
    return HarborChatClientConfig(
        default_model="primary",
        backend=backend,
        routing=routing or RoutingConfig(strategy=RoutingStrategy.ORDERED),
        observability=ObservabilityConfig(enabled=False),
        models={
            "primary": HarborChatModelConfig(deployments=(deployment_config,)),
        },
    )


def test_factory_builds_direct_sdk_backend() -> None:
    config = config_for(
        ChatBackendConfig(type=ChatBackendType.DIRECT_SDK),
        deployment(HarborProvider.OPENAI),
    )

    backend = build_chat_backend(config, ProviderRegistry.default())

    assert isinstance(backend, LiteLLMDirectBackend)
    backend.close()


def test_factory_builds_litellm_router_backend() -> None:
    captured: dict[str, Any] = {}

    class Router:
        def completion(self, **kwargs: Any) -> Any:
            return kwargs

        async def acompletion(self, **kwargs: Any) -> Any:
            return kwargs

    def router_factory(**kwargs: Any) -> Router:
        captured.update(kwargs)
        return Router()

    config = config_for(
        ChatBackendConfig(type=ChatBackendType.LITELLM_ROUTER),
        deployment(HarborProvider.OPENAI),
        routing=RoutingConfig(
            engine=RoutingEngine.LITELLM_ROUTER,
            strategy=RoutingStrategy.ORDERED,
        ),
    )

    backend = build_chat_backend(
        config,
        ProviderRegistry.default(),
        router_factory=router_factory,
    )

    assert isinstance(backend, LiteLLMRouterBackend)
    assert captured["num_retries"] == 0
    assert captured["max_fallbacks"] == 0
    backend.close()


def test_factory_builds_litellm_proxy_backend() -> None:
    config = config_for(
        ChatBackendConfig(
            type=ChatBackendType.LITELLM_PROXY,
            proxy=LiteLLMProxyConfig(
                api_base="https://proxy.example.test",
                api_key="proxy-key",
            ),
        ),
        deployment(HarborProvider.LITELLM_PROXY, model="gateway-chat"),
    )

    backend = build_chat_backend(config, ProviderRegistry.default())

    assert isinstance(backend, LiteLLMProxyBackend)
    backend.close()


def test_client_rejects_backend_routing_and_provider_mismatches() -> None:
    router_backend = ChatBackendConfig(type=ChatBackendType.LITELLM_ROUTER)
    invalid_router = config_for(router_backend, deployment(HarborProvider.OPENAI))
    with pytest.raises(HarborChatConfigurationError, match=r"routing\.engine"):
        HarborChatClient(invalid_router)

    proxy_backend = ChatBackendConfig(
        type=ChatBackendType.LITELLM_PROXY,
        proxy=LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-key",
        ),
    )
    invalid_proxy = config_for(proxy_backend, deployment(HarborProvider.OPENAI))
    with pytest.raises(HarborChatConfigurationError, match="litellm_proxy"):
        HarborChatClient(invalid_proxy)


def test_proxy_configuration_rejects_unsafe_values() -> None:
    with pytest.raises(ValueError, match="query or fragment"):
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test?token=secret",
            api_key="proxy-key",
        )
    with pytest.raises(ValueError, match="newlines"):
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-key",
            headers={"X-Team": "rag\r\nInjected: yes"},
        )
    with pytest.raises(ValueError, match="end with"):
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-key",
            model_prefix="proxy",
        )


def test_proxy_backend_authentication_and_metadata_propagation() -> None:
    config = LiteLLMProxyConfig(
        api_base="https://proxy.example.test",
        api_key="proxy-key",
        auth_mode=ProxyAuthMode.X_LITELLM_API_KEY,
        headers={"X-Static": "value"},
        metadata=ProxyMetadataConfig(
            tenant_id_header="X-Tenant",
            user_id_header="X-User",
        ),
    )
    backend = LiteLLMProxyBackend(
        config,
        connections=SharedConnectionLifecycle(ConnectionPoolConfig()),
        completion=lambda **kwargs: kwargs,
        acompletion=lambda **kwargs: kwargs,
    )
    params = backend.prepare_parameters(
        {
            "model": "primary",
            "api_key": "provider-key",
            "metadata": {
                "harborrag": {
                    "request_id": "request-1",
                    "trace_id": "trace-1",
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                }
            },
            "extra_headers": {"X-Request": "request"},
        }
    )
    assert params["model"] == "litellm_proxy/primary"
    assert params["api_key"] == "harbor-proxy-placeholder"
    assert params["api_base"] == "https://proxy.example.test"
    assert params["extra_headers"] == {
        "X-Static": "value",
        "x-litellm-api-key": "proxy-key",
        "x-harbor-request-id": "request-1",
        "x-harbor-trace-id": "trace-1",
        "X-Tenant": "tenant-1",
        "X-User": "user-1",
        "X-Request": "request",
    }
    backend.close()


def test_proxy_custom_header_validation_and_disabled_metadata() -> None:
    with pytest.raises(ValueError, match="auth_header_name"):
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-key",
            auth_mode=ProxyAuthMode.CUSTOM_HEADER,
        )
    with pytest.raises(ValueError, match="only valid"):
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-key",
            auth_header_name="X-Key",
        )
