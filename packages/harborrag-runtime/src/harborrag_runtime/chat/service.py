"""Runtime chat orchestration and client lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from harborrag_core.ports.model_clients import AsyncHarborChatClientProtocol
from harborrag_runtime.config.settings import RuntimeSettings

from .prompts import ChatPrompt, PromptCatalog

type ChatClientBuilder = Callable[[RuntimeSettings], AsyncHarborChatClientProtocol]


class RuntimeChatService:
    """Apply server-owned prompts and execute chat on one lazy client."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        client_builder: ChatClientBuilder | None = None,
        prompts: PromptCatalog | None = None,
    ) -> None:
        self._settings = settings
        self._client_builder = client_builder
        self._prompts = prompts or PromptCatalog.packaged()
        self._client: AsyncHarborChatClientProtocol | None = None
        self._client_lock = asyncio.Lock()

    async def complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> HarborChatResponse:
        prepared = self._apply_prompt(request, prompt)
        client = await self._configured_client()
        return await client.achat(request=prepared)

    async def stream(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        prepared = self._apply_prompt(request, prompt)
        client = await self._configured_client()
        async for chunk in client.astream(request=prepared):
            yield chunk

    async def aclose(self) -> None:
        """Swap and close the client under the same lock ``_configured_client`` uses.

        Without the lock, a concurrent ``_configured_client()`` call can miss
        the fast path, see the field already nulled out here, and build a
        second client that this call never closes -- a leak under concurrent
        shutdown.
        """
        async with self._client_lock:
            client, self._client = self._client, None
            if client is not None:
                await client.aclose()

    def _apply_prompt(
        self,
        request: HarborChatRequest,
        prompt: ChatPrompt | None,
    ) -> HarborChatRequest:
        if prompt is None:
            return request
        system_message = HarborChatMessage.system(self._prompts.resolve(prompt))
        return request.model_copy(update={"messages": (system_message, *request.messages)})

    async def _configured_client(self) -> AsyncHarborChatClientProtocol:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                builder = self._client_builder
                if builder is None:
                    from .composition import build_chat_client

                    builder = build_chat_client
                self._client = builder(self._settings)
        return self._client
