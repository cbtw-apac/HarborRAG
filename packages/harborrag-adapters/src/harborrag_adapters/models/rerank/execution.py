from __future__ import annotations

from typing import cast

from harborrag_core.models.errors import HarborRerankError
from harborrag_core.models.rerank import HarborRerankRequest, HarborRerankResponse

from harborrag_core.models.common.cache import CacheDecision, ModelResponseCache, ResponseCacheController
from harborrag_core.models.common.execution import RoutedAttempt, RoutedModelExecutor
from harborrag_core.models.common.telemetry import TelemetryDispatcher, litellm_telemetry_metadata
from harborrag_core.models.common.telemetry_operation import ModelTelemetryOperation
from .configs import HarborRerankClientConfig, HarborRerankProviderConfig
from .errors import normalize_exception
from .invocation import RerankInvocation
from .normalization import normalize_rerank_response
from .parameters import build_litellm_parameters
from .validation import validate_rerank_request


class RerankExecution:
    """Execute cached reranking through the shared routing state machine."""

    def __init__(
        self,
        config: HarborRerankClientConfig,
        invocation: RerankInvocation,
        *,
        cache: ModelResponseCache | None = None,
        telemetry: TelemetryDispatcher,
    ) -> None:
        self.config = config
        self.invocation = invocation
        self.owns_cache = cache is None
        self.cache = ResponseCacheController(config.cache, family="rerank", backend=cache)
        self.telemetry = telemetry
        self.router: RoutedModelExecutor[HarborRerankProviderConfig] = RoutedModelExecutor(
            config.models, routing=config.routing, retry=config.retry
        )

    def rerank(
        self,
        logical: str,
        request: HarborRerankRequest,
        *,
        model_alias: str,
    ) -> HarborRerankResponse:
        operation = self._operation(logical, request, model_alias)
        operation.start()
        decision = self.cache.decision(request, logical)
        if cached := self.cache.get(decision):
            operation.cache(decision, hit=True)
            response = cast(
                HarborRerankResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            operation.complete(response)
            return response
        operation.cache(decision, hit=False)
        try:
            result = self.router.execute(
                logical,
                invoke=lambda attempt: self.invocation.rerank(
                    **self._parameters(attempt, request, decision)
                ),
                normalize=lambda raw, name, deployment, latency: normalize_rerank_response(
                    raw,
                    request=request,
                    logical_model=name,
                    deployment=deployment,
                    request_id=_request_id(request),
                    latency_ms=latency,
                    retry_count=0,
                ),
                normalize_error=lambda exc, name, deployment: self._error(
                    exc, name, deployment, request
                ),
                on_transition=operation.transition,
            )
        except HarborRerankError as exc:
            operation.error(exc)
            raise
        self.cache.set(decision, result.value)
        operation.complete(result.value)
        return result.value

    async def arerank(
        self,
        logical: str,
        request: HarborRerankRequest,
        *,
        model_alias: str,
    ) -> HarborRerankResponse:
        operation = self._operation(logical, request, model_alias)
        await operation.astart()
        decision = self.cache.decision(request, logical)
        if cached := await self.cache.aget(decision):
            await operation.acache(decision, hit=True)
            response = cast(
                HarborRerankResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            await operation.acomplete(response)
            return response
        await operation.acache(decision, hit=False)
        try:
            result = await self.router.aexecute(
                logical,
                invoke=lambda attempt: self.invocation.arerank(
                    **self._parameters(attempt, request, decision)
                ),
                normalize=lambda raw, name, deployment, latency: normalize_rerank_response(
                    raw,
                    request=request,
                    logical_model=name,
                    deployment=deployment,
                    request_id=_request_id(request),
                    latency_ms=latency,
                    retry_count=0,
                ),
                normalize_error=lambda exc, name, deployment: self._error(
                    exc, name, deployment, request
                ),
                on_transition=operation.atransition,
            )
        except HarborRerankError as exc:
            await operation.aerror(exc)
            raise
        await self.cache.aset(decision, result.value)
        await operation.acomplete(result.value)
        return result.value

    def _parameters(
        self,
        attempt: RoutedAttempt[HarborRerankProviderConfig],
        request: HarborRerankRequest,
        decision: CacheDecision,
    ) -> dict[str, object]:
        routed = request.model_copy(update={"logical_model": attempt.logical_model})
        validate_rerank_request(routed, self.config, attempt.deployment)
        params = build_litellm_parameters(
            attempt.deployment,
            routed,
            timeout=self.config.timeouts.request_seconds,
        )
        params.update(self.cache.provider_parameters(decision))
        params["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="rerank",
            logical_model=attempt.logical_model,
        )
        return params

    @staticmethod
    def _error(
        exc: Exception,
        logical: str,
        deployment: HarborRerankProviderConfig,
        request: HarborRerankRequest,
    ) -> HarborRerankError:
        return normalize_exception(
            exc,
            logical_model=logical,
            provider=deployment.provider.value,
            provider_model=deployment.model,
            deployment=deployment.name,
            request_id=request.metadata.request_id,
        )

    def _operation(
        self, logical: str, request: HarborRerankRequest, model_alias: str
    ) -> ModelTelemetryOperation:
        return ModelTelemetryOperation(
            self.telemetry,
            operation="rerank",
            request=request,
            model_alias=model_alias,
            logical_model=logical,
        )


def _request_id(request: HarborRerankRequest) -> str:
    request_id = request.metadata.request_id
    if request_id is None:
        raise RuntimeError("prepared rerank request has no request ID")
    return request_id
