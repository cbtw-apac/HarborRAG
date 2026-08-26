from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, cast

from pydantic import BaseModel

from harborrag_core.models.chat import (
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from harborrag_core.ports.model_clients import HarborChatClientProtocol

from .batch_client import SyncChatBatchMixin
from .client_runtime import ChatClientRuntime
from .parameters import ChatMessageInput
from .structured import SyncStructuredOutputExecutor
from .structured_strategy import StructuredOutputStrategy


class HarborChatClient(
    SyncChatBatchMixin,
    ChatClientRuntime,
    HarborChatClientProtocol,
):
    """Run provider-independent synchronous chat completions."""

    def chat(
        self,
        messages: Sequence[ChatMessageInput] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate and normalize one synchronous chat completion."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return cast(
            HarborChatResponse,
            self._execution.chat(logical, prepared, model_alias=alias),
        )

    def chat_structured[StructuredResponseT: BaseModel](
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
        return SyncStructuredOutputExecutor(self, self.config).chat(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            strategy=strategy,
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
        """Yield normalized synchronous stream events with pre-event failover."""

        logical, prepared, alias = self._prepare(messages, request, model, kwargs)
        return self._stream_execution.stream(
            logical,
            prepared,
            model_alias=alias,
        )
