from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, Self, cast

from pydantic import BaseModel

from harborrag_adapters.models.runtime.budget import ModelBudgetPolicy
from harborrag_adapters.models.runtime.cache import ModelResponseCache
from harborrag_adapters.models.runtime.client_lifecycle import ModelClientLifecycleMixin
from harborrag_adapters.models.runtime.client_runtime import ModelClientRuntimeMixin
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_adapters.models.runtime.health import (
    ActiveHealthMonitor,
    DeploymentHealthProbe,
)
from harborrag_adapters.models.runtime.introspection import ModelRuntimeIntrospector
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_adapters.models.runtime.middleware import MiddlewarePipeline
from harborrag_adapters.models.runtime.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.runtime.routing_state import RoutingStateStore
from harborrag_adapters.models.runtime.runtime_services import (
    ModelRuntimeServices,
    build_runtime_services,
)
from harborrag_adapters.models.runtime.singleflight import SingleFlightCoordinator
from harborrag_adapters.models.runtime.telemetry import TelemetryDispatcher
from harborrag_core.models.chat import (
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from harborrag_core.models.protocols import (
    AsyncHarborChatClientProtocol,
    HarborChatClientProtocol,
)

from .backend import ChatBackend
from .backend_config import ChatBackendType
from .backends.factory import build_chat_backend
from .batch_client import HarborChatBatchMixin
from .client_resources import ChatClientResourcesMixin
from .configs import HarborChatClientConfig
from .execution import ChatExecution
from .invocation import ChatCompletionInvocation
from .parameters import ChatMessageInput, prepare_chat_request
from .registry import ProviderRegistry
from .stream_execution import ChatStreamExecution
from .structured import StructuredOutputExecutor
from .structured_strategy import StructuredOutputStrategy
from .validation import validate_chat_configuration


class HarborChatClient(
    HarborChatBatchMixin,
    ChatClientResourcesMixin,
    ModelClientRuntimeMixin,
    ModelClientLifecycleMixin,
    HarborChatClientProtocol,
    AsyncHarborChatClientProtocol,
):
    """Run provider-independent synchronous and asynchronous chat completions."""

    def __init__(
        self,
        config: HarborChatClientConfig,
        *,
        backend: ChatBackend | None = None,
        invocation: ChatCompletionInvocation | None = None,
        cache: ModelResponseCache | None = None,
        runtime_services: ModelRuntimeServices | None = None,
        routing_state: RoutingStateStore | None = None,
        singleflight: SingleFlightCoordinator | None = None,
        budget: ModelBudgetPolicy | None = None,
        redis: RedisConnectionLifecycle | None = None,
        services_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        health_probe: DeploymentHealthProbe | None = None,
        connections: SharedConnectionLifecycle | None = None,
        connection_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
        resource_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        telemetry: TelemetryDispatcher | None = None,
        telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
        provider_registry: ProviderRegistry | None = None,
        middleware: Sequence[object] = (),
    ) -> None:
        """Validate configuration and store injected runtime boundaries."""

        if backend is not None and invocation is not None:
            raise ValueError("backend and invocation are mutually exclusive")
        if connections is not None and (backend is not None or invocation is not None):
            raise ValueError("connections cannot be combined with an injected backend")
        if config.routing.active_health.start_automatically and health_probe is None:
            raise ValueError(
                "routing.active_health.start_automatically requires an injected health probe"
            )
        registry = provider_registry or ProviderRegistry.default()
        validate_chat_configuration(config, registry)
        self.config = config
        self._registry = registry
        self._middleware = MiddlewarePipeline(middleware)
        self._invocation = (
            backend
            or invocation
            or build_chat_backend(
                config,
                registry,
                connections=connections,
                connection_ownership=connection_ownership,
            )
        )
        self._telemetry = telemetry or TelemetryDispatcher((), config=config.observability)
        self._owns_telemetry = telemetry is None or telemetry_ownership is ResourceOwnership.OWNED
        self._services = runtime_services or build_runtime_services(
            config,
            family="chat",
            cache=cache,
            routing_state=routing_state,
            singleflight=singleflight,
            budget=budget,
            redis=redis,
        )
        self._owns_services = (
            runtime_services is None or services_ownership is ResourceOwnership.OWNED
        )
        self._execution = ChatExecution(
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
        self._stream_execution = ChatStreamExecution(
            config,
            self._invocation,
            self._telemetry,
            runtime=self._execution.router.runtime,
            registry=registry,
            middleware=self._middleware,
            budget=self._services.budget,
        )
        self._introspector = ModelRuntimeIntrospector(
            config,
            config.models,
            self._execution.router.runtime.selector,
            family="chat",
            backend=self._backend_type().value,
        )
        self._health_monitor = (
            ActiveHealthMonitor(
                config.models,
                config=config.routing.active_health,
                store=self._services.routing_state,
                probe=health_probe,
            )
            if health_probe is not None
            else None
        )
        if config.routing.active_health.start_automatically:
            self.start_health_monitor()
        self._resource_ownership = resource_ownership
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: HarborChatClientConfig,
        *,
        backend: ChatBackend | None = None,
        invocation: ChatCompletionInvocation | None = None,
        cache: ModelResponseCache | None = None,
        runtime_services: ModelRuntimeServices | None = None,
        routing_state: RoutingStateStore | None = None,
        singleflight: SingleFlightCoordinator | None = None,
        budget: ModelBudgetPolicy | None = None,
        redis: RedisConnectionLifecycle | None = None,
        services_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        health_probe: DeploymentHealthProbe | None = None,
        connections: SharedConnectionLifecycle | None = None,
        connection_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
        resource_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        telemetry: TelemetryDispatcher | None = None,
        telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
        provider_registry: ProviderRegistry | None = None,
        middleware: Sequence[object] = (),
    ) -> Self:
        """Construct a client from a validated Python configuration object."""

        return cls(
            config,
            backend=backend,
            invocation=invocation,
            cache=cache,
            runtime_services=runtime_services,
            routing_state=routing_state,
            singleflight=singleflight,
            budget=budget,
            redis=redis,
            services_ownership=services_ownership,
            health_probe=health_probe,
            connections=connections,
            connection_ownership=connection_ownership,
            resource_ownership=resource_ownership,
            telemetry=telemetry,
            telemetry_ownership=telemetry_ownership,
            provider_registry=provider_registry,
            middleware=middleware,
        )

    def chat(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate and normalize one synchronous chat completion."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return cast(
            HarborChatResponse,
            self._execution.chat(logical, prepared, model_alias=alias),
        )

    async def achat(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate and normalize one asynchronous chat completion."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return cast(
            HarborChatResponse,
            await self._execution.achat(logical, prepared, model_alias=alias),
        )

    def chat_structured[StructuredResponseT: BaseModel](
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        model: str | None = None,
        max_repair_attempts: int | None = None,
        strategy: StructuredOutputStrategy | None = None,
        **kwargs: Any,
    ) -> StructuredResponseT:
        """Generate, validate, and optionally repair one typed response."""

        self._ensure_open()
        return StructuredOutputExecutor(self, self.config).chat(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            strategy=strategy,
            request_kwargs=kwargs,
        )

    async def achat_structured[StructuredResponseT: BaseModel](
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        model: str | None = None,
        max_repair_attempts: int | None = None,
        strategy: StructuredOutputStrategy | None = None,
        **kwargs: Any,
    ) -> StructuredResponseT:
        """Generate, validate, and optionally repair one typed asynchronous response."""

        self._ensure_open()
        return await StructuredOutputExecutor(self, self.config).achat(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            strategy=strategy,
            request_kwargs=kwargs,
        )

    def stream(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[HarborChatStreamChunk]:
        """Yield normalized synchronous stream events with pre-event failover."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return self._stream_execution.stream(logical, prepared, model_alias=alias)

    def astream(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        """Yield normalized asynchronous stream events with pre-event failover."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return self._stream_execution.astream(logical, prepared, model_alias=alias)

    @property
    def backend_type(self) -> ChatBackendType:
        """Return the concrete transport backend selected for this client."""

        return self._backend_type()

    def _backend_type(self) -> ChatBackendType:
        value = getattr(self._invocation, "backend_type", ChatBackendType.DIRECT_SDK)
        return value if isinstance(value, ChatBackendType) else ChatBackendType(value)

    def _prepare(
        self,
        messages: Sequence[ChatMessageInput] | None,
        request: HarborChatRequest | None,
        model: str | None,
        kwargs: dict[str, Any],
    ) -> tuple[str, HarborChatRequest, str]:
        self._ensure_open()
        alias = model or (request.logical_model if request is not None else None)
        alias = alias or self.config.default_model
        logical, _deployment, prepared = prepare_chat_request(
            self.config,
            messages,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return logical, prepared, alias
