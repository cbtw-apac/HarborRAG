"""Public SDK façade for chat completion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse, HarborChatStreamChunk

from .prompts import ChatPrompt

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG


class ChatFacade:
    """Expose provider-neutral chat completion through the runtime SDK."""

    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> HarborChatResponse:
        return await self._owner._chat_complete(request, prompt=prompt)

    def stream(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        return self._owner._chat_stream(request, prompt=prompt)

    async def validate_model(self, model: str | None, *, tenant_id: str | None) -> None:
        """Raise ``HarborValidationError`` unless this tenant may use ``model``.

        The one authority on model selection: a tenant with its own catalog is
        bounded by it, everyone else by the process-wide catalog. Callers ask
        before they build a turn so a rejected name is a validation failure
        rather than a provider error mid-stream.
        """

        await self._owner._chat_validate_model(model, tenant_id=tenant_id)
