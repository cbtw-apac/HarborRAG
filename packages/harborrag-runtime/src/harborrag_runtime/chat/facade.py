"""Public SDK façade for chat completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse

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
