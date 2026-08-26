from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

from harborrag_adapters.models.runtime.budget import ModelBudgetPolicy
from harborrag_adapters.models.runtime.cache import CacheDecision
from harborrag_adapters.models.runtime.config import RoutingEngine
from harborrag_adapters.models.runtime.execution import (
    RoutingRuntime,
    normalize_execution_error,
    routing_unavailable_error,
)
from harborrag_adapters.models.runtime.litellm_router import router_model_name
from harborrag_adapters.models.runtime.middleware import (
    MiddlewarePipeline,
    middleware_context,
)
from harborrag_adapters.models.runtime.routing_state import RoutingAdmissionError
from harborrag_adapters.models.runtime.telemetry import (
    TelemetryDispatcher,
    TelemetryDispatchError,
    litellm_telemetry_metadata,
)
from harborrag_adapters.models.runtime.telemetry_operation import ModelTelemetryOperation
from harborrag_core.models.chat import HarborChatRequest, HarborChatStreamChunk
from harborrag_core.models.errors import HarborChatError, HarborModelError

from .backend import ChatBackend
from .configs import HarborChatClientConfig, HarborChatProviderConfig
from .errors import normalize_exception
from .parameters import build_litellm_parameters, chat_request_id
from .registry import ProviderRegistry
from .streaming import ChatStreamNormalizer
from .validation import validate_chat_request


class ChatStreamExecution:
    """Route chat streams before the first event and never replay partial output."""

    def __init__(
        self,
        config: HarborChatClientConfig,
        backend: ChatBackend,
        telemetry: TelemetryDispatcher,
        *,
        runtime: RoutingRuntime[HarborChatProviderConfig],
        registry: ProviderRegistry,
        middleware: MiddlewarePipeline,
        budget: ModelBudgetPolicy,
    ) -> None:
        """Reuse non-streaming health state and store stream-specific dependencies."""

        self.config = config
        self.backend = backend
        self.telemetry = telemetry
        self.runtime = runtime
        self.registry = registry
        self.middleware = middleware
        self.budget = budget

    def stream(
        self,
        logical: str,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> Iterator[HarborChatStreamChunk]:
        """Yield a synchronous stream with retries only before the first event."""

        context = middleware_context(
            operation="chat",
            logical_model=logical,
            model_alias=model_alias,
            request=request,
        )
        request = cast(HarborChatRequest, self.middleware.before(request, context))
        operation = self._operation(logical, request, model_alias)
        operation.start(streaming=True)
        operation.cache(CacheDecision(None, "streaming"), hit=False)
        authorization = self.budget.authorize(request, logical_model=logical)
        cursor = self.runtime.cursor(logical)
        last_error: HarborModelError | None = None
        while attempt := cursor.next_attempt_sync(self.runtime.selector):
            state = attempt.state
            routed = request.model_copy(update={"logical_model": attempt.logical_model})
            normalizer = self._normalizer(attempt.logical_model, state.config, routed)
            raw_stream: Any = None
            emitted = False
            try:
                with self.runtime.selector.lease_sync(
                    state,
                    logical_model=attempt.logical_model,
                    token_cost=authorization.estimated_tokens,
                ):
                    started = time.perf_counter()
                    raw_stream = self.backend.stream(
                        **self._parameters(attempt.logical_model, state.config, routed)
                    )
                    for raw in raw_stream:
                        for chunk in normalizer.consume(raw):
                            emitted = True
                            operation.stream_event(chunk)
                            yield chunk
                    completed = cast(
                        HarborChatStreamChunk,
                        self.middleware.after(normalizer.complete(), context),
                    )
                    emitted = True
                    self.runtime.selector.record_success_sync(state, _elapsed(started))
                    self.budget.settle(authorization, completed)
                    operation.complete(completed, streaming=True)
                    yield completed
                return
            except TelemetryDispatchError:
                raise
            except GeneratorExit:
                cancellation = RuntimeError("stream cancelled")
                self.middleware.error(cancellation, context)
                operation.error(cancellation, streaming=True)
                raise
            except Exception as exc:
                error = self._routed_error(exc, attempt.logical_model, state.config, routed)
                last_error = error
                retryable = bool(error.retryable)
                if not isinstance(exc, RoutingAdmissionError):
                    self.runtime.selector.record_failure_sync(state, retryable=retryable)
                if emitted:
                    self.middleware.error(error, context)
                    operation.error(error, streaming=True)
                    yield normalizer.error(error)
                    raise error from exc
                before = cursor.counts
                if not cursor.failed(
                    retryable=retryable,
                    current_available=state.available(time.monotonic()),
                ):
                    self.middleware.error(error, context)
                    operation.error(error, streaming=True)
                    yield normalizer.error(error)
                    raise error from exc
                operation.transition(cursor.transition(before, attempt.public, error))
                delay = self.runtime.retry.delay_seconds(cursor.retry_count)
                if delay:
                    time.sleep(delay)
            finally:
                if raw_stream is not None:
                    self.backend.close_stream(raw_stream)
        unavailable = routing_unavailable_error(logical, cursor, last_error)
        self.middleware.error(unavailable, context)
        operation.error(unavailable, streaming=True)
        raise unavailable

    async def astream(
        self,
        logical: str,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        """Yield an async stream with cancellation-safe pre-event failover."""

        context = middleware_context(
            operation="chat",
            logical_model=logical,
            model_alias=model_alias,
            request=request,
        )
        request = cast(HarborChatRequest, await self.middleware.abefore(request, context))
        operation = self._operation(logical, request, model_alias)
        await operation.astart(streaming=True)
        await operation.acache(CacheDecision(None, "streaming"), hit=False)
        authorization = await self.budget.aauthorize(request, logical_model=logical)
        cursor = self.runtime.cursor(logical)
        last_error: HarborModelError | None = None
        while attempt := await cursor.next_attempt(self.runtime.selector):
            state = attempt.state
            routed = request.model_copy(update={"logical_model": attempt.logical_model})
            normalizer = self._normalizer(attempt.logical_model, state.config, routed)
            raw_stream: Any = None
            emitted = False
            try:
                async with self.runtime.selector.lease(
                    state,
                    logical_model=attempt.logical_model,
                    token_cost=authorization.estimated_tokens,
                ):
                    started = time.perf_counter()
                    raw_stream = await self.backend.astream(
                        **self._parameters(attempt.logical_model, state.config, routed)
                    )
                    async for raw in raw_stream:
                        for chunk in normalizer.consume(raw):
                            emitted = True
                            await operation.astream_event(chunk)
                            yield chunk
                    completed = cast(
                        HarborChatStreamChunk,
                        await self.middleware.aafter(normalizer.complete(), context),
                    )
                    emitted = True
                    await self.runtime.selector.record_success(state, _elapsed(started))
                    await self.budget.asettle(authorization, completed)
                    await operation.acomplete(completed, streaming=True)
                    yield completed
                return
            except TelemetryDispatchError:
                raise
            except (asyncio.CancelledError, GeneratorExit):
                # GeneratorExit is a BaseException, not an Exception, so it
                # would otherwise skip the `except Exception` branch below
                # and reach `finally` without ever firing the error hooks --
                # this is the async equivalent of a caller abandoning the
                # generator (e.g. breaking out of an `async for` or letting
                # it get garbage-collected), matching the sync stream()'s
                # explicit `except GeneratorExit` handling above.
                cancellation = RuntimeError("stream cancelled")
                await self.middleware.aerror(cancellation, context)
                await operation.aerror(cancellation, streaming=True)
                raise
            except Exception as exc:
                error = self._routed_error(exc, attempt.logical_model, state.config, routed)
                last_error = error
                retryable = bool(error.retryable)
                if not isinstance(exc, RoutingAdmissionError):
                    await self.runtime.selector.record_failure(state, retryable=retryable)
                if emitted:
                    await self.middleware.aerror(error, context)
                    await operation.aerror(error, streaming=True)
                    yield normalizer.error(error)
                    raise error from exc
                before = cursor.counts
                if not cursor.failed(
                    retryable=retryable,
                    current_available=state.available(time.monotonic()),
                ):
                    await self.middleware.aerror(error, context)
                    await operation.aerror(error, streaming=True)
                    yield normalizer.error(error)
                    raise error from exc
                await operation.atransition(cursor.transition(before, attempt.public, error))
                await self.runtime.retry.sleep(cursor.retry_count)
            finally:
                if raw_stream is not None:
                    await asyncio.shield(self.backend.aclose_stream(raw_stream))
        unavailable = routing_unavailable_error(logical, cursor, last_error)
        await self.middleware.aerror(unavailable, context)
        await operation.aerror(unavailable, streaming=True)
        raise unavailable

    def _parameters(
        self,
        logical: str,
        deployment: HarborChatProviderConfig,
        request: HarborChatRequest,
    ) -> dict[str, Any]:
        validate_chat_request(request, self.config, deployment)
        override = (
            router_model_name(logical, deployment.name)
            if self.config.routing.engine is RoutingEngine.LITELLM_ROUTER
            else None
        )
        descriptor = self.registry.get(deployment.provider)
        parameters = build_litellm_parameters(
            deployment,
            request,
            timeout=self.config.timeouts.stream_seconds or self.config.timeouts.request_seconds,
            stream=True,
            model_override=override,
            litellm_provider=descriptor.litellm_provider,
        )
        parameters["caching"] = False
        parameters["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="chat",
            logical_model=logical,
            request_metadata=request.metadata,
            privacy=self.config.observability.privacy,
        )
        return parameters

    @staticmethod
    def _normalizer(
        logical: str,
        deployment: HarborChatProviderConfig,
        request: HarborChatRequest,
    ) -> ChatStreamNormalizer:
        return ChatStreamNormalizer(
            logical_model=logical,
            deployment=deployment,
            request_id=chat_request_id(request),
        )

    def _operation(
        self, logical: str, request: HarborChatRequest, model_alias: str
    ) -> ModelTelemetryOperation:
        return ModelTelemetryOperation(
            self.telemetry,
            operation="chat",
            request=request,
            model_alias=model_alias,
            logical_model=logical,
        )

    @staticmethod
    def _error(
        exc: Exception,
        logical: str,
        deployment: HarborChatProviderConfig,
        request: HarborChatRequest,
    ) -> HarborChatError:
        return normalize_exception(
            exc,
            provider=deployment.provider.value,
            logical_model=logical,
            provider_model=deployment.model,
            deployment=deployment.name,
            request_id=chat_request_id(request),
        )

    @classmethod
    def _routed_error(
        cls,
        exc: Exception,
        logical: str,
        deployment: HarborChatProviderConfig,
        request: HarborChatRequest,
    ) -> HarborModelError:
        return normalize_execution_error(
            exc,
            logical,
            deployment,
            lambda raw, name, target: cls._error(raw, name, target, request),
        )


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1_000
