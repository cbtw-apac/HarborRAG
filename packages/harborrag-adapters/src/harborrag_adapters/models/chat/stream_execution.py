from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

from harborrag_core.models.chat import (
    HarborChatRequest,
    HarborChatStreamChunk,
)
from harborrag_core.models.errors import HarborChatError

from harborrag_adapters.models.common.cache import CacheDecision
from harborrag_adapters.models.common.config import RoutingEngine
from harborrag_adapters.models.common.litellm_router import router_model_name
from harborrag_adapters.models.common.telemetry import (
    TelemetryDispatcher,
    TelemetryDispatchError,
    litellm_telemetry_metadata,
)
from harborrag_adapters.models.common.telemetry_operation import ModelTelemetryOperation
from .configs import HarborChatClientConfig, HarborChatProviderConfig
from .errors import normalize_exception
from .invocation import ChatCompletionInvocation
from .parameters import build_litellm_parameters, chat_request_id
from .streaming import ChatStreamNormalizer


class ChatStreamExecution:
    """Execute normalized chat streams with privacy-safe lifecycle telemetry."""

    def __init__(
        self,
        config: HarborChatClientConfig,
        invocation: ChatCompletionInvocation,
        telemetry: TelemetryDispatcher,
    ) -> None:
        """Store stream invocation and telemetry boundaries."""

        self.config = config
        self.invocation = invocation
        self.telemetry = telemetry

    def stream(
        self,
        logical: str,
        deployment: HarborChatProviderConfig,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> Iterator[HarborChatStreamChunk]:
        """Yield one synchronous stream and emit normalized stream events."""

        operation = self._operation(logical, request, model_alias)
        operation.start(streaming=True)
        operation.cache(CacheDecision(None, "streaming"), hit=False)
        normalizer = self._normalizer(logical, deployment, request)
        raw_stream: Any = None
        try:
            raw_stream = self.invocation.stream(**self._parameters(logical, deployment, request))
            for raw in raw_stream:
                for chunk in normalizer.consume(raw):
                    operation.stream_event(chunk)
                    yield chunk
            completed = normalizer.complete()
            operation.complete(completed, streaming=True)
            yield completed
        except TelemetryDispatchError:
            raise
        except GeneratorExit:
            operation.error(RuntimeError("stream cancelled"), streaming=True)
            raise
        except Exception as exc:
            error = self._error(exc, logical, deployment, request)
            operation.error(error, streaming=True)
            yield normalizer.error(error)
            if error is exc:
                raise error
            raise error from exc
        finally:
            if raw_stream is not None:
                self.invocation.close_stream(raw_stream)

    async def astream(
        self,
        logical: str,
        deployment: HarborChatProviderConfig,
        request: HarborChatRequest,
        *,
        model_alias: str,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        """Yield one asynchronous stream and emit normalized stream events."""

        operation = self._operation(logical, request, model_alias)
        await operation.astart(streaming=True)
        await operation.acache(CacheDecision(None, "streaming"), hit=False)
        normalizer = self._normalizer(logical, deployment, request)
        raw_stream: Any = None
        try:
            raw_stream = await self.invocation.astream(
                **self._parameters(logical, deployment, request)
            )
            async for raw in raw_stream:
                for chunk in normalizer.consume(raw):
                    await operation.astream_event(chunk)
                    yield chunk
            completed = normalizer.complete()
            await operation.acomplete(completed, streaming=True)
            yield completed
        except TelemetryDispatchError:
            raise
        except asyncio.CancelledError:
            await operation.aerror(RuntimeError("stream cancelled"), streaming=True)
            raise
        except Exception as exc:
            error = self._error(exc, logical, deployment, request)
            await operation.aerror(error, streaming=True)
            yield normalizer.error(error)
            if error is exc:
                raise error
            raise error from exc
        finally:
            if raw_stream is not None:
                await asyncio.shield(self.invocation.aclose_stream(raw_stream))

    def _parameters(
        self,
        logical: str,
        deployment: HarborChatProviderConfig,
        request: HarborChatRequest,
    ) -> dict[str, Any]:
        override = (
            router_model_name(logical, deployment.name)
            if self.config.routing.engine is RoutingEngine.LITELLM_ROUTER
            else None
        )
        parameters = build_litellm_parameters(
            deployment,
            request,
            timeout=self.config.stream_timeout_seconds or self.config.timeouts.request_seconds,
            stream=True,
            model_override=override,
        )
        parameters["caching"] = False
        parameters["metadata"] = litellm_telemetry_metadata(
            request_id=request.metadata.request_id,
            operation="chat",
            logical_model=logical,
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
