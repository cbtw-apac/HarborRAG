from __future__ import annotations

from typing import Any, Self

from harborrag_adapters.models.runtime.client_lifecycle import ModelClientLifecycleMixin
from harborrag_adapters.models.runtime.client_runtime import ModelClientRuntimeMixin
from harborrag_adapters.models.runtime.config import RoutingEngine
from harborrag_adapters.models.runtime.introspection import ModelRuntimeIntrospector
from harborrag_adapters.models.runtime.lifecycle import (
    AsyncLifecycleResource,
    LifecycleResource,
    ResourceOwnership,
)
from harborrag_adapters.models.runtime.litellm_router import build_litellm_router
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import (
    HarborEmbedRequest,
    HarborEmbedResponse,
    RawEmbeddingInput,
)
from harborrag_core.ports.model_clients import (
    AsyncHarborEmbedClientProtocol,
    HarborEmbedClientProtocol,
)

from .configs import HarborEmbedClientConfig
from .execution import EmbedExecution
from .invocation import (
    EmbeddingInvocation,
    LiteLLMEmbeddingInvocation,
    LiteLLMEmbeddingRouterInvocation,
)
from .parameters import prepare_embed_request
from .registry import EmbedProviderRegistry
from .schemas import EmbedClientDependencies
from .validation import validate_embed_configuration


class HarborEmbedClient(
    ModelClientRuntimeMixin,
    ModelClientLifecycleMixin,
    HarborEmbedClientProtocol,
    AsyncHarborEmbedClientProtocol,
):
    """Provide synchronous and asynchronous provider-neutral embeddings."""

    def __init__(
        self,
        config: HarborEmbedClientConfig,
        dependencies: EmbedClientDependencies | None = None,
    ) -> None:
        """Validate configuration and store injected embedding runtime boundaries."""

        selected = dependencies or EmbedClientDependencies()
        self._require_health_probe(config, selected)
        registry = selected.provider_registry or EmbedProviderRegistry.default()
        validate_embed_configuration(config, registry)
        self.config = config
        self._registry = registry
        self._invocation = selected.invocation or self._default_invocation(config, registry)
        self._resolve_shared_runtime(config, selected, family="embed")
        self._execution = EmbedExecution(
            config,
            self._invocation,
            registry=registry,
            middleware=self._middleware,
            cache=self._services.cache,
            telemetry=self._telemetry,
            routing_state=self._services.routing_state,
            singleflight=self._services.singleflight,
            budget=self._services.budget,
        )
        self._introspector = ModelRuntimeIntrospector(
            config,
            config.models,
            self._execution.router.runtime.selector,
            family="embed",
            backend=type(self._invocation).__name__,
        )
        self._resolve_health_monitor(config, selected, models=config.models)
        self._resource_ownership = selected.resource_ownership
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: HarborEmbedClientConfig,
        dependencies: EmbedClientDependencies | None = None,
    ) -> Self:
        """Construct an embedding client from a validated configuration object."""

        return cls(config, dependencies)

    def embed(
        self,
        inputs: RawEmbeddingInput | None = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        """Embed one input or batch synchronously in stable input order."""

        logical, prepared, alias = self._prepare(inputs, request, model, kwargs)
        return self._execution.embed(logical, prepared, model_alias=alias)

    async def aembed(
        self,
        inputs: RawEmbeddingInput | None = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        """Embed one input or batch asynchronously in stable input order."""

        logical, prepared, alias = self._prepare(inputs, request, model, kwargs)
        return await self._execution.aembed(logical, prepared, model_alias=alias)

    def capabilities(self, model: str | None = None) -> dict[str, HarborEmbedCapabilities]:
        """Return declared capabilities for each deployment of a logical model."""

        logical_name, _ = self.config.model_for(model)
        return {
            deployment.name: deployment.capabilities
            for deployment in self.config.models[logical_name].deployments
        }

    def _prepare(
        self,
        inputs: RawEmbeddingInput | None,
        request: HarborEmbedRequest | None,
        model: str | None,
        kwargs: dict[str, Any],
    ) -> tuple[str, HarborEmbedRequest, str]:
        self._ensure_open()
        alias = model or (request.logical_model if request is not None else None)
        alias = alias or self.config.default_model
        logical, _deployment, prepared = prepare_embed_request(
            self.config,
            inputs,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return logical, prepared, alias

    def _sync_resources(self) -> tuple[LifecycleResource, ...]:
        resources: list[LifecycleResource] = []
        if self._health_monitor is not None:
            resources.append(LifecycleResource(self._health_monitor.close))
        resources.extend(
            (
                LifecycleResource(self._invocation.close, self._resource_ownership),
                LifecycleResource(
                    self._execution.cache.backend.close,
                    (
                        ResourceOwnership.OWNED
                        if self._execution.owns_cache
                        else ResourceOwnership.BORROWED
                    ),
                ),
                LifecycleResource(
                    self._telemetry.close,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_telemetry
                        else ResourceOwnership.BORROWED
                    ),
                ),
                LifecycleResource(
                    self._services.close,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_services
                        else ResourceOwnership.BORROWED
                    ),
                ),
            )
        )
        return tuple(resources)

    def _async_resources(self) -> tuple[AsyncLifecycleResource, ...]:
        resources: list[AsyncLifecycleResource] = []
        if self._health_monitor is not None:
            resources.append(AsyncLifecycleResource(self._health_monitor.aclose))
        resources.extend(
            (
                AsyncLifecycleResource(self._invocation.aclose, self._resource_ownership),
                AsyncLifecycleResource(
                    self._execution.cache.backend.aclose,
                    (
                        ResourceOwnership.OWNED
                        if self._execution.owns_cache
                        else ResourceOwnership.BORROWED
                    ),
                ),
                AsyncLifecycleResource(
                    self._telemetry.aclose,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_telemetry
                        else ResourceOwnership.BORROWED
                    ),
                ),
                AsyncLifecycleResource(
                    self._services.aclose,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_services
                        else ResourceOwnership.BORROWED
                    ),
                ),
            )
        )
        return tuple(resources)

    @staticmethod
    def _default_invocation(
        config: HarborEmbedClientConfig, registry: EmbedProviderRegistry
    ) -> EmbeddingInvocation:
        if config.routing.engine is not RoutingEngine.LITELLM_ROUTER:
            return LiteLLMEmbeddingInvocation()
        router = build_litellm_router(
            config,
            config.models,
            provider_resolver=lambda deployment: registry.get(deployment.provider).litellm_provider,
        )
        return LiteLLMEmbeddingRouterInvocation(router)
