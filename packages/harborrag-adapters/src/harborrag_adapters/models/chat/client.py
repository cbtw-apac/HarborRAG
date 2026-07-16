from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, Self
from pydantic import BaseModel

from harborrag_core.models.chat import (
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from harborrag_core.models.protocols import AsyncHarborChatClientProtocol, HarborChatClientProtocol

from harborrag_core.models.common.cache import ModelResponseCache
from harborrag_core.models.common.config import RoutingEngine
from harborrag_core.models.common.lifecycle import ResourceOwnership
from harborrag_core.models.common.litellm_router import build_litellm_router
from harborrag_core.models.common.telemetry import TelemetryDispatcher

from .configs import HarborChatClientConfig
from .execution import ChatExecution
from .invocation import (
    ChatCompletionInvocation,
    LiteLLMChatInvocation,
    LiteLLMChatRouterInvocation,
)
from .parameters import (
    ChatMessageInput,
    prepare_chat_request,
)
from .stream_execution import ChatStreamExecution
from .structured import StructuredOutputExecutor
from .validation import validate_chat_configuration


class HarborChatClient(HarborChatClientProtocol, AsyncHarborChatClientProtocol):
    """Run provider-independent synchronous and asynchronous chat completions."""

    def __init__(
        self,
        config: HarborChatClientConfig,
        *,
        invocation: ChatCompletionInvocation | None = None,
        cache: ModelResponseCache | None = None,
        resource_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        telemetry: TelemetryDispatcher | None = None,
        telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
    ) -> None:
        """Validate configuration and store the injected LiteLLM invocation boundary."""

        validate_chat_configuration(config)
        self.config = config
        self._invocation = invocation or self._default_invocation(config)
        self._telemetry = telemetry or TelemetryDispatcher((), config=config.observability)
        self._owns_telemetry = telemetry is None or telemetry_ownership is ResourceOwnership.OWNED
        self._execution = ChatExecution(
            config,
            self._invocation,
            cache=cache,
            telemetry=self._telemetry,
        )
        self._stream_execution = ChatStreamExecution(config, self._invocation, self._telemetry)
        self._resource_ownership = resource_ownership
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: HarborChatClientConfig,
        *,
        invocation: ChatCompletionInvocation | None = None,
        cache: ModelResponseCache | None = None,
        resource_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        telemetry: TelemetryDispatcher | None = None,
        telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
    ) -> Self:
        """Construct a client from a validated Python configuration object."""

        return cls(
            config,
            invocation=invocation,
            cache=cache,
            resource_ownership=resource_ownership,
            telemetry=telemetry,
            telemetry_ownership=telemetry_ownership,
        )

    def __enter__(self) -> Self:
        """Enter the synchronous lifecycle context."""

        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close owned invocation resources on synchronous context exit."""

        self.close()

    async def __aenter__(self) -> Self:
        """Enter the asynchronous lifecycle context."""

        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close owned invocation resources on asynchronous context exit."""

        await self.aclose()

    def chat(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate and normalize one synchronous text completion."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical_name, _, prepared = prepare_chat_request(
            self.config,
            messages,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return self._execution.chat(logical_name, prepared, model_alias=model_alias)

    async def achat(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate and normalize one asynchronous text completion."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical_name, _, prepared = prepare_chat_request(
            self.config,
            messages,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return await self._execution.achat(logical_name, prepared, model_alias=model_alias)

    def chat_structured[StructuredResponseT: BaseModel](
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        model: str | None = None,
        max_repair_attempts: int | None = None,
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
        **kwargs: Any,
    ) -> StructuredResponseT:
        """Generate, validate, and optionally repair one typed async response."""

        self._ensure_open()
        return await StructuredOutputExecutor(self, self.config).achat(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
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
        """Yield normalized synchronous text, tool, usage, and completion events."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical_name, deployment, prepared = prepare_chat_request(
            self.config,
            messages,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        yield from self._stream_execution.stream(
            logical_name,
            deployment,
            prepared,
            model_alias=model_alias,
        )

    def astream(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        """Yield normalized asynchronous text, tool, usage, and completion events."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical_name, deployment, prepared = prepare_chat_request(
            self.config,
            messages,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return self._stream_execution.astream(
            logical_name,
            deployment,
            prepared,
            model_alias=model_alias,
        )

    def close(self) -> None:
        """Close an owned invocation exactly once."""

        if self._closed:
            return
        self._closed = True
        if self._resource_ownership is ResourceOwnership.OWNED:
            self._invocation.close()
        if self._execution.owns_cache:
            self._execution.cache.backend.close()
        if self._owns_telemetry:
            self._telemetry.close()

    async def aclose(self) -> None:
        """Asynchronously close an owned invocation exactly once."""

        if self._closed:
            return
        self._closed = True
        if self._resource_ownership is ResourceOwnership.OWNED:
            await self._invocation.aclose()
        if self._execution.owns_cache:
            await self._execution.cache.backend.aclose()
        if self._owns_telemetry:
            await self._telemetry.aclose()

    @staticmethod
    def _default_invocation(config: HarborChatClientConfig) -> ChatCompletionInvocation:
        if config.routing.engine is not RoutingEngine.LITELLM_ROUTER:
            return LiteLLMChatInvocation()
        from .registry import ProviderRegistry

        registry = ProviderRegistry.default()
        router = build_litellm_router(
            config,
            config.models,
            provider_resolver=lambda deployment: registry.get(deployment.provider).litellm_provider,
        )
        return LiteLLMChatRouterInvocation(router)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("HarborChatClient is closed")


AsyncHarborChatClient = HarborChatClient

__all__ = ["AsyncHarborChatClient", "HarborChatClient"]
