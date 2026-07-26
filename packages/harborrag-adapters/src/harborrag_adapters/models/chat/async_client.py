from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from pydantic import BaseModel

from harborrag_core.models.chat import (
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from harborrag_core.ports.model_clients import AsyncHarborChatClientProtocol

from .batch_client import AsyncChatBatchMixin
from .client_runtime import ChatClientRuntime
from .parameters import ChatMessageInput
from .structured import AsyncStructuredOutputExecutor
from .structured_strategy import StructuredOutputStrategy


class AsyncHarborChatClient(
    AsyncChatBatchMixin,
    ChatClientRuntime,
    AsyncHarborChatClientProtocol,
):
    """Run provider-independent asynchronous chat completions."""

    async def achat(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate and normalize one asynchronous chat completion."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return cast(
            HarborChatResponse,
            await self._execution.achat(logical, prepared, model_alias=alias),
        )

    async def achat_structured[StructuredResponseT: BaseModel](
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        model: str | None = None,
        max_repair_attempts: int | None = None,
        strategy: StructuredOutputStrategy | None = None,
        **kwargs: Any,
    ) -> StructuredResponseT:
        """Generate, validate, and optionally repair one typed response."""

        self._ensure_open()
        return await AsyncStructuredOutputExecutor(self, self.config).achat(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            strategy=strategy,
            request_kwargs=kwargs,
        )

    def astream(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        """Yield normalized asynchronous stream events with pre-event failover."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return self._stream_execution.astream(
            logical,
            prepared,
            model_alias=alias,
        )
