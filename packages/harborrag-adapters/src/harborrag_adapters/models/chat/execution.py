from __future__ import annotations

from typing import cast

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse

from harborrag_adapters.models.common.budget import (
    BudgetAuthorization,
    ModelBudgetPolicy,
)
from harborrag_adapters.models.common.cache import (
    CacheDecision,
    ModelResponseCache,
    ResponseCacheController,
)
from harborrag_adapters.models.common.config import RoutingEngine
from harborrag_adapters.models.common.execution import RoutedModelExecutor
from harborrag_adapters.models.common.litellm_router import router_model_name
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

from .configs import HarborChatClientConfig, HarborChatProviderConfig
from .errors import normalize_exception
from .invocation import ChatCompletionInvocation
from .normalization import normalize_chat_response
from .parameters import build_litellm_parameters, chat_request_id
from .registry import ProviderRegistry
from .validation import validate_chat_request


class ChatExecution:
    """Execute cached chat completions through the shared routing state machine."""

    def __init__(
        self,
        config: HarborChatClientConfig,
        invocation: ChatCompletionInvocation,
        *,
        registry: ProviderRegistry,
        middleware: MiddlewarePipeline,
        cache: ModelResponseCache | None = None,
        telemetry: TelemetryDispatcher,
        routing_state: RoutingStateStore,
        singleflight: SingleFlightCoordinator,
        budget: ModelBudgetPolicy,
    ) -> None:
        """Store runtime dependencies and create shared deployment health state."""

        self.config = config
        self.invocation = invocation
        self.registry = registry
        self.middleware = middleware
        self.owns_cache = cache is None
        self.cache = ResponseCacheController(config.cache, family="chat", backend=cache)
        self.telemetry = telemetry
        self.singleflight = singleflight
        self.budget = budget
        self.router: RoutedModelExecutor[HarborChatProviderConfig] = RoutedModelExecutor(
            config.models,
            routing=config.routing,
            retry=config.retry,
            state_store=routing_state,
        )

    def chat(
        self,
        logical: str,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> HarborChatResponse:
        """Run one synchronous request through policy, deduplication, and routing."""

        context = middleware_context(
            operation="chat",
            logical_model=logical,
            model_alias=model_alias,
            request=request,
        )
        request = cast(HarborChatRequest, self.middleware.before(request, context))
        operation = self._operation(logical, request, model_alias)
        operation.start()
        decision = self.cache.decision(request, logical)
        if cached := self.cache.get(decision):
            operation.cache(decision, hit=True)
            response = cast(
                HarborChatResponse,
                self.cache.mark_hit(cached, request_id=chat_request_id(request)),
            )
            response = cast(HarborChatResponse, self.middleware.after(response, context))
            operation.complete(response)
            return response
        operation.cache(decision, hit=False)
        authorization = self.budget.authorize(request, logical_model=logical)
        try:
            shared = self.singleflight.execute(
                decision.key or f"chat:{chat_request_id(request)}",
                lambda: self._produce(logical, request, decision, operation, authorization),
                lambda: cast(HarborChatResponse | None, self.cache.get(decision)),
            )
            raw = (
                cast(
                    HarborChatResponse,
                    self.cache.mark_shared(shared.value, request_id=chat_request_id(request)),
                )
                if shared.shared
                else shared.value
            )
            response = cast(HarborChatResponse, self.middleware.after(raw, context))
            self.budget.settle(authorization, response)
        except Exception as exc:
            self.middleware.error(exc, context)
            operation.error(exc)
            raise
        operation.complete(response)
        return response

    async def achat(
        self,
        logical: str,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> HarborChatResponse:
        """Run one asynchronous request through policy, deduplication, and routing."""

        context = middleware_context(
            operation="chat",
            logical_model=logical,
            model_alias=model_alias,
            request=request,
        )
        request = cast(HarborChatRequest, await self.middleware.abefore(request, context))
        operation = self._operation(logical, request, model_alias)
        await operation.astart()
        decision = self.cache.decision(request, logical)
        if cached := await self.cache.aget(decision):
            await operation.acache(decision, hit=True)
            response = cast(
                HarborChatResponse,
                self.cache.mark_hit(cached, request_id=chat_request_id(request)),
            )
            response = cast(HarborChatResponse, await self.middleware.aafter(response, context))
            await operation.acomplete(response)
            return response
        await operation.acache(decision, hit=False)
        authorization = await self.budget.aauthorize(request, logical_model=logical)
        try:
            shared = await self.singleflight.aexecute(
                decision.key or f"chat:{chat_request_id(request)}",
                lambda: self._aproduce(logical, request, decision, operation, authorization),
                lambda: self._cached_chat(decision),
            )
            raw = (
                cast(
                    HarborChatResponse,
                    self.cache.mark_shared(shared.value, request_id=chat_request_id(request)),
                )
                if shared.shared
                else shared.value
            )
            response = cast(HarborChatResponse, await self.middleware.aafter(raw, context))
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
        request: HarborChatRequest,
        decision: CacheDecision,
        operation: ModelTelemetryOperation,
        authorization: BudgetAuthorization,
    ) -> HarborChatResponse:
        result = self.router.execute(
            logical,
            invoke=lambda attempt: self.invocation.complete(
                **self._parameters(attempt, request, decision)
            ),
            normalize=lambda raw, name, deployment, latency: normalize_chat_response(
                raw,
                logical_model=name,
                deployment=deployment,
                request_id=chat_request_id(request),
                latency_ms=latency,
            ),
            normalize_error=lambda exc, name, deployment: normalize_exception(
                exc,
                provider=deployment.provider.value,
                logical_model=name,
                provider_model=deployment.model,
                deployment=deployment.name,
                request_id=chat_request_id(request),
            ),
            on_transition=operation.transition,
            estimated_tokens=authorization.estimated_tokens,
        )
        self.cache.set(decision, result.value)
        return result.value

    async def _aproduce(
        self,
        logical: str,
        request: HarborChatRequest,
        decision: CacheDecision,
        operation: ModelTelemetryOperation,
        authorization: BudgetAuthorization,
    ) -> HarborChatResponse:
        result = await self.router.aexecute(
            logical,
            invoke=lambda attempt: self.invocation.acomplete(
                **self._parameters(attempt, request, decision)
            ),
            normalize=lambda raw, name, deployment, latency: normalize_chat_response(
                raw,
                logical_model=name,
                deployment=deployment,
                request_id=chat_request_id(request),
                latency_ms=latency,
            ),
            normalize_error=lambda exc, name, deployment: normalize_exception(
                exc,
                provider=deployment.provider.value,
                logical_model=name,
                provider_model=deployment.model,
                deployment=deployment.name,
                request_id=chat_request_id(request),
            ),
            on_transition=operation.atransition,
            estimated_tokens=authorization.estimated_tokens,
        )
        await self.cache.aset(decision, result.value)
        return result.value

    async def _cached_chat(self, decision: CacheDecision) -> HarborChatResponse | None:
        return cast(HarborChatResponse | None, await self.cache.aget(decision))

    def _parameters(
        self,
        attempt: RoutedAttempt[HarborChatProviderConfig],
        request: HarborChatRequest,
        decision: CacheDecision,
    ) -> dict[str, object]:
        routed_request = request.model_copy(update={"logical_model": attempt.logical_model})
        validate_chat_request(routed_request, self.config, attempt.deployment)
        model_override = (
            router_model_name(attempt.logical_model, attempt.deployment.name)
            if self.config.routing.engine is RoutingEngine.LITELLM_ROUTER
            else None
        )
        descriptor = self.registry.get(attempt.deployment.provider)
        parameters = build_litellm_parameters(
            attempt.deployment,
            routed_request,
            timeout=self.config.timeouts.request_seconds,
            model_override=model_override,
            litellm_provider=descriptor.litellm_provider,
        )
        parameters.update(self.cache.provider_parameters(decision))
        parameters["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="chat",
            logical_model=attempt.logical_model,
            request_metadata=request.metadata,
            privacy=self.config.observability.privacy,
        )
        return parameters

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
