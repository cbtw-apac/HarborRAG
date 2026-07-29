from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harborrag_adapters.models.runtime.client_lifecycle import (
    ModelClientLifecycleMixin,
)
from harborrag_adapters.models.runtime.client_runtime import ModelClientRuntimeMixin
from harborrag_adapters.models.runtime.introspection import ModelRuntimeIntrospector
from harborrag_core.models.chat import HarborChatRequest

from .backend_config import ChatBackendType
from .backends.factory import build_chat_backend
from .client_resources import ChatClientResourcesMixin
from .configs import HarborChatClientConfig
from .execution import ChatExecution
from .parameters import ChatMessageInput, prepare_chat_request
from .registry import ProviderRegistry
from .schemas import ChatClientDependencies
from .stream_execution import ChatStreamExecution
from .validation import validate_chat_configuration


class ChatClientRuntime(
    ChatClientResourcesMixin,
    ModelClientRuntimeMixin,
    ModelClientLifecycleMixin,
):
    """Compose shared chat execution and lifecycle boundaries for public clients."""

    def __init__(
        self,
        config: HarborChatClientConfig,
        dependencies: ChatClientDependencies | None = None,
    ) -> None:
        selected = dependencies or ChatClientDependencies()
        self._validate_dependencies(config, selected)
        registry = selected.provider_registry or ProviderRegistry.default()
        validate_chat_configuration(config, registry)

        self.config = config
        self._registry = registry
        self._backend = self._build_backend(config, selected, registry)
        self._resolve_shared_runtime(config, selected, family="chat")
        self._build_execution()
        self._build_introspection(selected)
        self._resource_ownership = selected.resource_ownership
        self._closed = False

    def _validate_dependencies(
        self,
        config: HarborChatClientConfig,
        dependencies: ChatClientDependencies,
    ) -> None:
        if dependencies.connections is not None and dependencies.backend is not None:
            raise ValueError("connections cannot be combined with an injected backend")
        self._require_health_probe(config, dependencies)

    @staticmethod
    def _build_backend(
        config: HarborChatClientConfig,
        dependencies: ChatClientDependencies,
        registry: ProviderRegistry,
    ) -> object:
        return dependencies.backend or build_chat_backend(
            config,
            registry,
            connections=dependencies.connections,
            connection_ownership=dependencies.connection_ownership,
        )

    def _build_execution(self) -> None:
        self._execution = ChatExecution(
            self.config,
            self._backend,
            registry=self._registry,
            middleware=self._middleware,
            cache=self._services.cache,
            telemetry=self._telemetry,
            routing_state=self._services.routing_state,
            singleflight=self._services.singleflight,
            budget=self._services.budget,
        )
        self._stream_execution = ChatStreamExecution(
            self.config,
            self._backend,
            self._telemetry,
            runtime=self._execution.router.runtime,
            registry=self._registry,
            middleware=self._middleware,
            budget=self._services.budget,
        )

    def _build_introspection(self, dependencies: ChatClientDependencies) -> None:
        self._introspector = ModelRuntimeIntrospector(
            self.config,
            self.config.models,
            self._execution.router.runtime.selector,
            family="chat",
            backend=self._backend_type().value,
        )
        self._resolve_health_monitor(self.config, dependencies, models=self.config.models)

    @property
    def backend_type(self) -> ChatBackendType:
        """Return the concrete transport backend selected for this client."""

        return self._backend_type()

    def _backend_type(self) -> ChatBackendType:
        value = getattr(
            self._backend,
            "backend_type",
            ChatBackendType.DIRECT_SDK,
        )
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
