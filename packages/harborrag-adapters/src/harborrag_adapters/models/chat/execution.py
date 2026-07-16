from __future__ import annotations

from typing import cast

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse
from harborrag_core.models.errors import HarborChatError

from harborrag_adapters.models.common.cache import CacheDecision, ModelResponseCache, ResponseCacheController
from harborrag_adapters.models.common.config import RoutingEngine
from harborrag_adapters.models.common.execution import RoutedAttempt, RoutedModelExecutor
from harborrag_adapters.models.common.litellm_router import router_model_name
from harborrag_adapters.models.common.telemetry import TelemetryDispatcher, litellm_telemetry_metadata
from harborrag_adapters.models.common.telemetry_operation import ModelTelemetryOperation

from .configs import HarborChatClientConfig, HarborChatProviderConfig
from .errors import normalize_exception
from .invocation import ChatCompletionInvocation
from .normalization import normalize_chat_response
from .parameters import build_litellm_parameters, chat_request_id
from .validation import validate_chat_request


class ChatExecution:
    """Execute cached chat completions through the shared routing state machine."""

    def __init__(
        self,
        config: HarborChatClientConfig,
        invocation: ChatCompletionInvocation,
        *,
        cache: ModelResponseCache | None = None,
        telemetry: TelemetryDispatcher,
    ) -> None:
        self.config = config
        self.invocation = invocation
        self.owns_cache = cache is None
        self.cache = ResponseCacheController(config.cache, family="chat", backend=cache)
        self.telemetry = telemetry
        self.router: RoutedModelExecutor[HarborChatProviderConfig] = RoutedModelExecutor(
            config.models, routing=config.routing, retry=config.retry
        )

    def chat(
        self,
        logical: str,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> HarborChatResponse:
        operation = self._operation(logical, request, model_alias)
        operation.start()
        decision = self.cache.decision(request, logical)
        if cached := self.cache.get(decision):
            operation.cache(decision, hit=True)
            response = cast(
                HarborChatResponse,
                self.cache.mark_hit(cached, request_id=chat_request_id(request)),
            )
            operation.complete(response)
            return response
        operation.cache(decision, hit=False)
        try:
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
            )
        except HarborChatError as exc:
            operation.error(exc)
            raise
        self.cache.set(decision, result.value)
        operation.complete(result.value)
        return result.value

    async def achat(
        self,
        logical: str,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> HarborChatResponse:
        operation = self._operation(logical, request, model_alias)
        await operation.astart()
        decision = self.cache.decision(request, logical)
        if cached := await self.cache.aget(decision):
            await operation.acache(decision, hit=True)
            response = cast(
                HarborChatResponse,
                self.cache.mark_hit(cached, request_id=chat_request_id(request)),
            )
            await operation.acomplete(response)
            return response
        await operation.acache(decision, hit=False)
        try:
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
            )
        except HarborChatError as exc:
            await operation.aerror(exc)
            raise
        await self.cache.aset(decision, result.value)
        await operation.acomplete(result.value)
        return result.value

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
        parameters = build_litellm_parameters(
            attempt.deployment,
            routed_request,
            timeout=self.config.timeouts.request_seconds,
            model_override=model_override,
        )
        parameters.update(self.cache.provider_parameters(decision))
        parameters["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="chat",
            logical_model=attempt.logical_model,
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
