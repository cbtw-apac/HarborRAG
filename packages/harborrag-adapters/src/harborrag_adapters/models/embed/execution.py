from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import cast

from harborrag_core.models.embed import HarborEmbedRequest, HarborEmbedResponse
from harborrag_core.models.errors import HarborEmbedError, HarborEmbedPartialBatchError

from harborrag_core.models.common.cache import CacheDecision, ModelResponseCache, ResponseCacheController
from harborrag_core.models.common.config import RoutingEngine
from harborrag_core.models.common.execution import RoutedAttempt, RoutedModelExecutor
from harborrag_core.models.common.litellm_router import router_model_name
from harborrag_core.models.common.telemetry import TelemetryDispatcher, litellm_telemetry_metadata
from harborrag_core.models.common.telemetry_operation import ModelTelemetryOperation
from .configs import HarborEmbedClientConfig, HarborEmbedProviderConfig
from .errors import normalize_exception
from .invocation import EmbeddingInvocation
from .normalization import merge_embedding_batches, normalize_embedding_batch
from .parameters import build_litellm_parameters, effective_batch_size, litellm_inputs
from .validation import validate_embed_request


@dataclass
class EmbeddingBatchAccumulator:
    """Collect one deployment attempt without exposing partial vectors."""

    logical_model: str
    embedding_space: str
    deployment: HarborEmbedProviderConfig
    request: HarborEmbedRequest
    responses: list[HarborEmbedResponse] = field(default_factory=list)

    def add(self, raw: object, *, offset: int, size: int, latency_ms: float) -> None:
        self.responses.append(
            normalize_embedding_batch(
                raw,
                input_count=size,
                index_offset=offset,
                logical_model=self.logical_model,
                embedding_space=self.embedding_space,
                deployment=self.deployment,
                request_id=self.request_id,
                latency_ms=latency_ms,
                normalize_vectors=bool(self.request.normalize),
            )
        )

    @property
    def request_id(self) -> str:
        request_id = self.request.metadata.request_id
        if request_id is None:
            raise RuntimeError("prepared embedding request has no request ID")
        return request_id

    def failure(self, error: HarborEmbedError, *, batch_index: int, completed: int) -> Exception:
        if completed == 0:
            return error
        return HarborEmbedPartialBatchError(
            "embedding batch failed after partial completion; no partial response was returned",
            operation="embed",
            provider=self.deployment.provider.value,
            logical_model=self.logical_model,
            provider_model=self.deployment.model,
            deployment=self.deployment.name,
            request_id=self.request_id,
            retryable=False,
            original_exception=error,
            metadata={
                "failed_batch_index": batch_index,
                "completed_inputs": completed,
                "total_inputs": len(self.request.inputs),
            },
        )

    def complete(self, latency_ms: float) -> HarborEmbedResponse:
        return merge_embedding_batches(
            self.responses,
            request_id=self.request_id,
            total_latency_ms=latency_ms,
            retry_count=0,
        )


class EmbedExecution:
    """Route and cache one complete embedding operation with stable batching."""

    def __init__(
        self,
        config: HarborEmbedClientConfig,
        invocation: EmbeddingInvocation,
        *,
        cache: ModelResponseCache | None = None,
        telemetry: TelemetryDispatcher,
    ) -> None:
        self.config = config
        self.invocation = invocation
        self.owns_cache = cache is None
        self.cache = ResponseCacheController(config.cache, family="embed", backend=cache)
        self.telemetry = telemetry
        self.router: RoutedModelExecutor[HarborEmbedProviderConfig] = RoutedModelExecutor(
            config.models, routing=config.routing, retry=config.retry
        )

    def embed(
        self,
        logical: str,
        request: HarborEmbedRequest,
        *,
        model_alias: str,
    ) -> HarborEmbedResponse:
        operation = self._operation(logical, request, model_alias)
        operation.start()
        decision = self.cache.decision(request, logical)
        if cached := self.cache.get(decision):
            operation.cache(decision, hit=True)
            response = cast(
                HarborEmbedResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            operation.complete(response)
            return response
        operation.cache(decision, hit=False)
        try:
            result = self.router.execute(
                logical,
                invoke=lambda attempt: self._invoke(attempt, request, decision),
                normalize=lambda raw, _name, _deployment, _latency: cast(HarborEmbedResponse, raw),
                normalize_error=lambda exc, name, deployment: self._error(
                    exc, name, deployment, request
                ),
                on_transition=operation.transition,
            )
        except HarborEmbedError as exc:
            operation.error(exc)
            raise
        self.cache.set(decision, result.value)
        operation.complete(result.value)
        return result.value

    async def aembed(
        self,
        logical: str,
        request: HarborEmbedRequest,
        *,
        model_alias: str,
    ) -> HarborEmbedResponse:
        operation = self._operation(logical, request, model_alias)
        await operation.astart()
        decision = self.cache.decision(request, logical)
        if cached := await self.cache.aget(decision):
            await operation.acache(decision, hit=True)
            response = cast(
                HarborEmbedResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            await operation.acomplete(response)
            return response
        await operation.acache(decision, hit=False)
        try:
            result = await self.router.aexecute(
                logical,
                invoke=lambda attempt: self._ainvoke(attempt, request, decision),
                normalize=lambda raw, _name, _deployment, _latency: cast(HarborEmbedResponse, raw),
                normalize_error=lambda exc, name, deployment: self._error(
                    exc, name, deployment, request
                ),
                on_transition=operation.atransition,
            )
        except HarborEmbedError as exc:
            await operation.aerror(exc)
            raise
        await self.cache.aset(decision, result.value)
        await operation.acomplete(result.value)
        return result.value

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
        return (
            EmbeddingBatchAccumulator(
                attempt.logical_model,
                model.embedding_space or attempt.logical_model,
                attempt.deployment,
                request,
            ),
            batches,
        )

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
        params = build_litellm_parameters(
            attempt.deployment,
            request,
            inputs=inputs,
            timeout=self.config.timeouts.request_seconds,
            model_override=override,
        )
        cache_params = self.cache.provider_parameters(decision)
        if "preset_cache_key" in cache_params:
            cache_params["preset_cache_key"] = f"{cache_params['preset_cache_key']}:{offset}"
        params.update(cache_params)
        params["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="embed",
            logical_model=attempt.logical_model,
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
