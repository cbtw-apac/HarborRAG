from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harborrag_adapters.models.common.connections import SharedConnectionLifecycle
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.common.litellm_router import build_litellm_router

from ..backend import ChatBackend
from ..backend_config import ChatBackendType
from ..configs import HarborChatClientConfig
from ..registry import ProviderRegistry
from .direct import LiteLLMDirectBackend
from .proxy import LiteLLMProxyBackend
from .router import LiteLLMRouterBackend

type RouterFactory = Callable[..., Any]


def build_chat_backend(
    config: HarborChatClientConfig,
    registry: ProviderRegistry,
    *,
    connections: SharedConnectionLifecycle | None = None,
    connection_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
    router_factory: RouterFactory | None = None,
) -> ChatBackend:
    """Build the configured backend without creating mutable module-level state."""

    lifecycle = connections or SharedConnectionLifecycle(config.connections)
    ownership = ResourceOwnership.OWNED if connections is None else connection_ownership
    backend_type = config.backend.resolved_type(config.routing.engine)
    if backend_type is ChatBackendType.DIRECT_SDK:
        return LiteLLMDirectBackend(
            connections=lifecycle,
            connection_ownership=ownership,
        )
    if backend_type is ChatBackendType.LITELLM_ROUTER:
        router = build_litellm_router(
            config,
            config.models,
            provider_resolver=lambda deployment: registry.get(deployment.provider).litellm_provider,
            router_factory=router_factory,
        )
        return LiteLLMRouterBackend(
            router,
            connections=lifecycle,
            connection_ownership=ownership,
        )
    proxy = config.backend.proxy
    if proxy is None:
        raise ValueError("LiteLLM Proxy backend requires backend.proxy configuration")
    return LiteLLMProxyBackend(
        proxy,
        connections=lifecycle,
        connection_ownership=ownership,
    )
