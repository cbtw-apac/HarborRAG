from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Self

from harborrag_adapters.models.runtime.client_lifecycle import ModelClientLifecycleMixin
from harborrag_adapters.models.runtime.client_runtime import ModelClientRuntimeMixin
from harborrag_adapters.models.runtime.health import ActiveHealthMonitor
from harborrag_adapters.models.runtime.introspection import ModelRuntimeIntrospector
from harborrag_adapters.models.runtime.lifecycle import (
    AsyncLifecycleResource,
    LifecycleResource,
    ResourceOwnership,
)
from harborrag_adapters.models.runtime.middleware import MiddlewarePipeline
from harborrag_adapters.models.runtime.runtime_services import build_runtime_services
from harborrag_adapters.models.runtime.telemetry import TelemetryDispatcher
from harborrag_core.models.capabilities import HarborRerankCapabilities
from harborrag_core.models.rerank import (
    HarborRerankRequest,
    HarborRerankResponse,
    RawRerankDocument,
)
from harborrag_core.ports.model_clients import (
    AsyncHarborRerankClientProtocol,
    HarborRerankClientProtocol,
)

from .configs import HarborRerankClientConfig
from .execution import RerankExecution
from .invocation import LiteLLMRerankInvocation
from .parameters import prepare_rerank_request
from .registry import RerankProviderRegistry
from .schemas import RerankClientDependencies
from .validation import validate_rerank_configuration


class HarborRerankingClient(
    ModelClientRuntimeMixin,
    ModelClientLifecycleMixin,
    HarborRerankClientProtocol,
    AsyncHarborRerankClientProtocol,
):
    """Provide synchronous and asynchronous provider-neutral reranking."""

    def __init__(
        self,
        config: HarborRerankClientConfig,
        dependencies: RerankClientDependencies | None = None,
    ) -> None:
        """Validate configuration and store injected reranking runtime boundaries."""

        selected = dependencies or RerankClientDependencies()
        if config.routing.active_health.start_automatically and selected.health_probe is None:
            raise ValueError(
                "routing.active_health.start_automatically requires an injected health probe"
            )
        registry = selected.provider_registry or RerankProviderRegistry.default()
        validate_rerank_configuration(config, registry)
        self.config = config
        self._registry = registry
        self._middleware = MiddlewarePipeline(selected.middleware)
        self._invocation = selected.invocation or LiteLLMRerankInvocation()
        self._telemetry = selected.telemetry or TelemetryDispatcher((), config=config.observability)
        self._owns_telemetry = (
            selected.telemetry is None or selected.telemetry_ownership is ResourceOwnership.OWNED
        )
        self._services = selected.runtime_services or build_runtime_services(
            config,
            family="rerank",
            cache=selected.cache,
            routing_state=selected.routing_state,
            singleflight=selected.singleflight,
            budget=selected.budget,
            redis=selected.redis,
        )
        self._owns_services = (
            selected.runtime_services is None
            or selected.services_ownership is ResourceOwnership.OWNED
        )
        self._execution = RerankExecution(
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
            family="rerank",
            backend=type(self._invocation).__name__,
        )
        self._health_monitor = (
            ActiveHealthMonitor(
                config.models,
                config=config.routing.active_health,
                store=self._services.routing_state,
                probe=selected.health_probe,
            )
            if selected.health_probe is not None
            else None
        )
        if config.routing.active_health.start_automatically:
            self.start_health_monitor()
        self._resource_ownership = selected.resource_ownership
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: HarborRerankClientConfig,
        dependencies: RerankClientDependencies | None = None,
    ) -> Self:
        """Construct a reranking client from a validated configuration object."""

        return cls(config, dependencies)

    def rerank(
        self,
        query: str | None = None,
        documents: Sequence[RawRerankDocument] | None = None,
        *,
        request: HarborRerankRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborRerankResponse:
        """Rerank a complete candidate set synchronously."""

        logical, prepared, alias = self._prepare(query, documents, request, model, kwargs)
        return self._execution.rerank(logical, prepared, model_alias=alias)

    async def arerank(
        self,
        query: str | None = None,
        documents: Sequence[RawRerankDocument] | None = None,
        *,
        request: HarborRerankRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborRerankResponse:
        """Rerank a complete candidate set asynchronously."""

        logical, prepared, alias = self._prepare(query, documents, request, model, kwargs)
        return await self._execution.arerank(logical, prepared, model_alias=alias)

    def capabilities(self, model: str | None = None) -> dict[str, HarborRerankCapabilities]:
        """Return declared capabilities for each deployment of a logical model."""

        logical_name, _ = self.config.model_for(model)
        return {
            deployment.name: deployment.capabilities
            for deployment in self.config.models[logical_name].deployments
        }

    def _prepare(
        self,
        query: str | None,
        documents: Sequence[RawRerankDocument] | None,
        request: HarborRerankRequest | None,
        model: str | None,
        kwargs: dict[str, Any],
    ) -> tuple[str, HarborRerankRequest, str]:
        self._ensure_open()
        alias = model or (request.logical_model if request is not None else None)
        alias = alias or self.config.default_model
        logical, _deployment, prepared = prepare_rerank_request(
            self.config,
            query,
            documents,
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
