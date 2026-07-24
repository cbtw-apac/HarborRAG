from __future__ import annotations

from typing import cast

from harborrag_adapters.models.common.budget import (
    BudgetAuthorization,
    ModelBudgetPolicy,
)
from harborrag_adapters.models.common.cache import (
    CacheDecision,
    ModelResponseCache,
    ResponseCacheController,
)
from harborrag_adapters.models.common.execution import RoutedModelExecutor
from harborrag_adapters.models.common.middleware import (
    MiddlewarePipeline,
    middleware_context,
)
from harborrag_adapters.models.common.routing_state import RoutingStateStore
from harborrag_adapters.models.common.routing_types import RoutedAttempt
from harborrag_adapters.models.common.singleflight import SingleFlightCoordinator
from harborrag_adapters.models.common.telemetry import (
    TelemetryDispatcher,
    litellm_telemetry_metadata,
)
from harborrag_adapters.models.common.telemetry_operation import ModelTelemetryOperation
from harborrag_core.models.errors import HarborRerankError
from harborrag_core.models.rerank import HarborRerankRequest, HarborRerankResponse

from .configs import HarborRerankClientConfig, HarborRerankProviderConfig
from .errors import normalize_exception
from .invocation import RerankInvocation
from .normalization import normalize_rerank_response
from .parameters import build_litellm_parameters
from .registry import RerankProviderRegistry
from .validation import validate_rerank_request


class RerankExecution:
    """Route, deduplicate, budget, and cache complete reranking operations."""

    def __init__(
        self,
        config: HarborRerankClientConfig,
        invocation: RerankInvocation,
        *,
        registry: RerankProviderRegistry,
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
        self.cache = ResponseCacheController(config.cache, family="rerank", backend=cache)
        self.telemetry = telemetry
        self.singleflight = singleflight
        self.budget = budget
        self.router: RoutedModelExecutor[HarborRerankProviderConfig] = RoutedModelExecutor(
            config.models,
            routing=config.routing,
            retry=config.retry,
            state_store=routing_state,
        )

    def rerank(
        self, logical: str, request: HarborRerankRequest, *, model_alias: str
    ) -> HarborRerankResponse:
        """Run one synchronous rerank request through all runtime policies."""

        context = middleware_context(
            operation="rerank",
            logical_model=logical,
            model_alias=model_alias,
            request=request,
        )
        request = cast(HarborRerankRequest, self.middleware.before(request, context))
        operation = self._operation(logical, request, model_alias)
        operation.start()
        decision = self.cache.decision(request, logical)
        if cached := self.cache.get(decision):
            operation.cache(decision, hit=True)
            response = cast(
                HarborRerankResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            response = cast(HarborRerankResponse, self.middleware.after(response, context))
            operation.complete(response)
            return response
        operation.cache(decision, hit=False)
        try:
            authorization = self.budget.authorize(request, logical_model=logical)
            if decision.key is None:
                raw = self._produce(logical, request, decision, operation, authorization)
            else:
                shared = self.singleflight.execute(
                    decision.key,
                    lambda: self._produce(logical, request, decision, operation, authorization),
                    lambda: cast(HarborRerankResponse | None, self.cache.get(decision)),
                )
                raw = (
                    cast(
                        HarborRerankResponse,
                        self.cache.mark_shared(
                            shared.value, request_id=request.metadata.request_id
                        ),
                    )
                    if shared.shared
                    else shared.value
                )
            response = cast(HarborRerankResponse, self.middleware.after(raw, context))
            self.budget.settle(authorization, response)
        except Exception as exc:
            self.middleware.error(exc, context)
            operation.error(exc)
            raise
        operation.complete(response)
        return response

    async def arerank(
        self, logical: str, request: HarborRerankRequest, *, model_alias: str
    ) -> HarborRerankResponse:
        """Run one asynchronous rerank request through all runtime policies."""

        context = middleware_context(
            operation="rerank",
            logical_model=logical,
            model_alias=model_alias,
            request=request,
        )
        request = cast(HarborRerankRequest, await self.middleware.abefore(request, context))
        operation = self._operation(logical, request, model_alias)
        await operation.astart()
        decision = self.cache.decision(request, logical)
        if cached := await self.cache.aget(decision):
            await operation.acache(decision, hit=True)
            response = cast(
                HarborRerankResponse,
                self.cache.mark_hit(cached, request_id=request.metadata.request_id),
            )
            response = cast(HarborRerankResponse, await self.middleware.aafter(response, context))
            await operation.acomplete(response)
            return response
        await operation.acache(decision, hit=False)
        try:
            authorization = await self.budget.aauthorize(request, logical_model=logical)
            if decision.key is None:
                raw = await self._aproduce(logical, request, decision, operation, authorization)
            else:
                shared = await self.singleflight.aexecute(
                    decision.key,
                    lambda: self._aproduce(logical, request, decision, operation, authorization),
                    lambda: self._cached(decision),
                )
                raw = (
                    cast(
                        HarborRerankResponse,
                        self.cache.mark_shared(
                            shared.value, request_id=request.metadata.request_id
                        ),
                    )
                    if shared.shared
                    else shared.value
                )
            response = cast(HarborRerankResponse, await self.middleware.aafter(raw, context))
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
        request: HarborRerankRequest,
        decision: CacheDecision,
        operation: ModelTelemetryOperation,
        authorization: BudgetAuthorization,
    ) -> HarborRerankResponse:
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
            estimated_tokens=authorization.estimated_tokens,
        )
        self.cache.set(decision, result.value)
        return result.value

    async def _aproduce(
        self,
        logical: str,
        request: HarborRerankRequest,
        decision: CacheDecision,
        operation: ModelTelemetryOperation,
        authorization: BudgetAuthorization,
    ) -> HarborRerankResponse:
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
            estimated_tokens=authorization.estimated_tokens,
        )
        await self.cache.aset(decision, result.value)
        return result.value

    async def _cached(self, decision: CacheDecision) -> HarborRerankResponse | None:
        return cast(HarborRerankResponse | None, await self.cache.aget(decision))

    def _parameters(
        self,
        attempt: RoutedAttempt[HarborRerankProviderConfig],
        request: HarborRerankRequest,
        decision: CacheDecision,
    ) -> dict[str, object]:
        routed = request.model_copy(update={"logical_model": attempt.logical_model})
        validate_rerank_request(routed, self.config, attempt.deployment)
        descriptor = self.registry.get(attempt.deployment.provider)
        params = build_litellm_parameters(
            attempt.deployment,
            routed,
            timeout=self.config.timeouts.request_seconds,
            litellm_provider=descriptor.litellm_provider,
        )
        params.update(self.cache.provider_parameters(decision))
        params["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="rerank",
            logical_model=attempt.logical_model,
            request_metadata=request.metadata,
            privacy=self.config.observability.privacy,
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
