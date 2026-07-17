from __future__ import annotations

import time
from typing import cast

from harborrag_core.models.embed import HarborEmbedRequest, HarborEmbedResponse
from harborrag_core.models.errors import HarborEmbedError

from harborrag_adapters.models.common.budget import BudgetAuthorization, ModelBudgetPolicy
from harborrag_adapters.models.common.cache import (
    CacheDecision,
    ModelResponseCache,
    ResponseCacheController,
)
from harborrag_adapters.models.common.config import RoutingEngine
from harborrag_adapters.models.common.execution import RoutedModelExecutor
from harborrag_adapters.models.common.litellm_router import router_model_name
from harborrag_adapters.models.common.middleware import MiddlewarePipeline, middleware_context
from harborrag_adapters.models.common.routing_state import RoutingStateStore
from harborrag_adapters.models.common.routing_types import RoutedAttempt
from harborrag_adapters.models.common.singleflight import SingleFlightCoordinator
from harborrag_adapters.models.common.telemetry import (
    TelemetryDispatcher,
    litellm_telemetry_metadata,
)
from harborrag_adapters.models.common.telemetry_operation import ModelTelemetryOperation

from .batching import EmbeddingBatchAccumulator
from .configs import HarborEmbedClientConfig, HarborEmbedProviderConfig
from .errors import normalize_exception
from .invocation import EmbeddingInvocation
from .parameters import build_litellm_parameters, effective_batch_size, litellm_inputs
from .registry import EmbedProviderRegistry
from .validation import validate_embed_request


class EmbedExecution:
    """Route, deduplicate, budget, and cache complete embedding operations."""

    def __init__(
        self,
        config: HarborEmbedClientConfig,
        invocation: EmbeddingInvocation,
        *,
        registry: EmbedProviderRegistry,
        middleware: MiddlewarePipeline,
        cache: ModelResponseCache | None,
        telemetry: TelemetryDispatcher,
        routing_state: RoutingStateStore,
        singleflight: SingleFlightCoordinator,
        budget: ModelBudgetPolicy,
    ) -> None:
        """Store injected runtime services and build the routed executor."""

        self.config = config
        self.invocation = invocation
        self.registry = registry
        self.middleware = middleware
        self.owns_cache = cache is None
        self.cache = ResponseCacheController(config.cache, family="embed", backend=cache)
        self.telemetry = telemetry
        self.singleflight = singleflight
        self.budget = budget
        self.router: RoutedModelExecutor[HarborEmbedProviderConfig] = RoutedModelExecutor(
            config.models,
            routing=config.routing,
            retry=config.retry,
            state_store=routing_state,
        )

    def embed(
        self, logical: str, request: HarborEmbedRequest, *, model_alias: str
    ) -> HarborEmbedResponse:
        """Run one synchronous embedding request through all runtime policies."""

        context = middleware_context(
            operation="embed", logical_model=logical, model_alias=model_alias, request=request
        )
        request = cast(HarborEmbedRequest, self.middleware.before(request, context))
        operation = self._operation(logical, request, model_alias)
        operation.start()
        decision = self.cache.decision(request, logical)
        if cached := self.cache.get(decision):
            operation.cache(decision, hit=True)
            response = cast(
                HarborEmbedResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            response = cast(HarborEmbedResponse, self.middleware.after(response, context))
            operation.complete(response)
            return response
        operation.cache(decision, hit=False)
        authorization = self.budget.authorize(request, logical_model=logical)
        try:
            shared = self.singleflight.execute(
                decision.key or f"embed:{request.metadata.request_id}",
                lambda: self._produce(logical, request, decision, operation, authorization),
                lambda: cast(HarborEmbedResponse | None, self.cache.get(decision)),
            )
            raw = (
                cast(
                    HarborEmbedResponse,
                    self.cache.mark_shared(shared.value, request_id=request.metadata.request_id),
                )
                if shared.shared
                else shared.value
            )
            response = cast(HarborEmbedResponse, self.middleware.after(raw, context))
            self.budget.settle(authorization, response)
        except Exception as exc:
            self.middleware.error(exc, context)
            operation.error(exc)
            raise
        operation.complete(response)
        return response

    async def aembed(
        self, logical: str, request: HarborEmbedRequest, *, model_alias: str
    ) -> HarborEmbedResponse:
        """Run one asynchronous embedding request through all runtime policies."""

        context = middleware_context(
            operation="embed", logical_model=logical, model_alias=model_alias, request=request
        )
        request = cast(HarborEmbedRequest, await self.middleware.abefore(request, context))
        operation = self._operation(logical, request, model_alias)
        await operation.astart()
        decision = self.cache.decision(request, logical)
        if cached := await self.cache.aget(decision):
            await operation.acache(decision, hit=True)
            response = cast(
                HarborEmbedResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            response = cast(HarborEmbedResponse, await self.middleware.aafter(response, context))
            await operation.acomplete(response)
            return response
        await operation.acache(decision, hit=False)
        authorization = await self.budget.aauthorize(request, logical_model=logical)
        try:
            shared = await self.singleflight.aexecute(
                decision.key or f"embed:{request.metadata.request_id}",
                lambda: self._aproduce(logical, request, decision, operation, authorization),
                lambda: self._cached(decision),
            )
            raw = (
                cast(
                    HarborEmbedResponse,
                    self.cache.mark_shared(shared.value, request_id=request.metadata.request_id),
                )
                if shared.shared
                else shared.value
            )
            response = cast(HarborEmbedResponse, await self.middleware.aafter(raw, context))
            await self.budget.asettle(authorization, response)
        except Exception as exc:
            await self.middleware.aerror(exc, context)
            await operation.aerror(exc)
            raise
        await operation.acomplete(response)
        return response

    def _produce(
        self,
        logical: str,
        request: HarborEmbedRequest,
        decision: CacheDecision,
        operation: ModelTelemetryOperation,
        authorization: BudgetAuthorization,
    ) -> HarborEmbedResponse:
        result = self.router.execute(
            logical,
            invoke=lambda attempt: self._invoke(attempt, request, decision),
            normalize=lambda raw, _name, _deployment, _latency: cast(HarborEmbedResponse, raw),
            normalize_error=lambda exc, name, deployment: self._error(
                exc, name, deployment, request
            ),
            on_transition=operation.transition,
            estimated_tokens=authorization.estimated_tokens,
        )
        self.cache.set(decision, result.value)
        return result.value

    async def _aproduce(
        self,
        logical: str,
        request: HarborEmbedRequest,
        decision: CacheDecision,
        operation: ModelTelemetryOperation,
        authorization: BudgetAuthorization,
    ) -> HarborEmbedResponse:
        result = await self.router.aexecute(
            logical,
            invoke=lambda attempt: self._ainvoke(attempt, request, decision),
            normalize=lambda raw, _name, _deployment, _latency: cast(HarborEmbedResponse, raw),
            normalize_error=lambda exc, name, deployment: self._error(
                exc, name, deployment, request
            ),
            on_transition=operation.atransition,
            estimated_tokens=authorization.estimated_tokens,
        )
        await self.cache.aset(decision, result.value)
        return result.value

    async def _cached(self, decision: CacheDecision) -> HarborEmbedResponse | None:
        return cast(HarborEmbedResponse | None, await self.cache.aget(decision))

    def _invoke(
        self,
        attempt: RoutedAttempt[HarborEmbedProviderConfig],
        request: HarborEmbedRequest,
        decision: CacheDecision,
    ) -> HarborEmbedResponse:
        routed = validate_embed_request(
            request.model_copy(update={"logical_model": attempt.logical_model}),
            self.config,
            attempt.deployment,
        )
        state, batches = self._batches(attempt, routed)
        operation_started = time.perf_counter()
        for batch_index, (offset, batch) in enumerate(batches):
            started = time.perf_counter()
            try:
                raw = self.invocation.embed(
                    **self._parameters(attempt, routed, batch, decision, offset)
                )
                state.add(raw, offset=offset, size=len(batch), latency_ms=_elapsed(started))
            except Exception as exc:
                error = self._error(exc, attempt.logical_model, attempt.deployment, request)
                raise state.failure(error, batch_index=batch_index, completed=offset) from exc
        return state.complete(_elapsed(operation_started))

    async def _ainvoke(
        self,
        attempt: RoutedAttempt[HarborEmbedProviderConfig],
        request: HarborEmbedRequest,
        decision: CacheDecision,
    ) -> HarborEmbedResponse:
        routed = validate_embed_request(
            request.model_copy(update={"logical_model": attempt.logical_model}),
            self.config,
            attempt.deployment,
        )
        state, batches = self._batches(attempt, routed)
        operation_started = time.perf_counter()
        for batch_index, (offset, batch) in enumerate(batches):
            started = time.perf_counter()
            try:
                raw = await self.invocation.aembed(
                    **self._parameters(attempt, routed, batch, decision, offset)
                )
                state.add(raw, offset=offset, size=len(batch), latency_ms=_elapsed(started))
            except Exception as exc:
                error = self._error(exc, attempt.logical_model, attempt.deployment, request)
                raise state.failure(error, batch_index=batch_index, completed=offset) from exc
        return state.complete(_elapsed(operation_started))

    def _batches(
        self,
        attempt: RoutedAttempt[HarborEmbedProviderConfig],
        request: HarborEmbedRequest,
    ) -> tuple[EmbeddingBatchAccumulator, list[tuple[int, list[str | list[int]]]]]:
        values = litellm_inputs(request)
        size = effective_batch_size(self.config, attempt.deployment, request)
        batches = [
            (offset, values[offset : offset + size]) for offset in range(0, len(values), size)
        ]
        model = self.config.models[attempt.logical_model]
        return EmbeddingBatchAccumulator(
            attempt.logical_model,
            model.embedding_space or attempt.logical_model,
            attempt.deployment,
            request,
        ), batches

    def _parameters(
        self,
        attempt: RoutedAttempt[HarborEmbedProviderConfig],
        request: HarborEmbedRequest,
        inputs: list[str | list[int]],
        decision: CacheDecision,
        offset: int,
    ) -> dict[str, object]:
        override = (
            router_model_name(attempt.logical_model, attempt.deployment.name)
            if self.config.routing.engine is RoutingEngine.LITELLM_ROUTER
            else None
        )
        descriptor = self.registry.get(attempt.deployment.provider)
        params = build_litellm_parameters(
            attempt.deployment,
            request,
            inputs=inputs,
            timeout=self.config.timeouts.request_seconds,
            model_override=override,
            litellm_provider=descriptor.litellm_provider,
        )
        cache_params = self.cache.provider_parameters(decision)
        if "preset_cache_key" in cache_params:
            cache_params["preset_cache_key"] = f"{cache_params['preset_cache_key']}:{offset}"
        params.update(cache_params)
        params["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="embed",
            logical_model=attempt.logical_model,
            request_metadata=request.metadata,
            privacy=self.config.observability.privacy,
        )
        return params

    @staticmethod
    def _error(
        exc: Exception,
        logical: str,
        deployment: HarborEmbedProviderConfig,
        request: HarborEmbedRequest,
    ) -> HarborEmbedError:
        return normalize_exception(
            exc,
            logical_model=logical,
            provider=deployment.provider.value,
            provider_model=deployment.model,
            deployment=deployment.name,
            request_id=request.metadata.request_id,
        )

    def _operation(
        self, logical: str, request: HarborEmbedRequest, model_alias: str
    ) -> ModelTelemetryOperation:
        return ModelTelemetryOperation(
            self.telemetry,
            operation="embed",
            request=request,
            model_alias=model_alias,
            logical_model=logical,
        )


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1_000
