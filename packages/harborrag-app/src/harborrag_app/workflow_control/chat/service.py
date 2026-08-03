"""Application service for authenticated chat completion."""

from __future__ import annotations

import logging
from collections.abc import Callable

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.models.chat import HarborChatRequest
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.sdk import HarborRAG

from .presenters import chat_response_data

type RuntimeProvider = Callable[[], HarborRAG]

logger = logging.getLogger("harborrag.app.workflow_control.chat")


class ChatApplicationService:
    """Attach access identity and project runtime chat responses."""

    def __init__(self, runtime_provider: RuntimeProvider) -> None:
        self._runtime_provider = runtime_provider

    async def complete(
        self,
        request: HarborChatRequest,
        *,
        tenant_id: str,
        principal_id: str,
        prompt: ChatPrompt | None = None,
    ) -> AppResponse:
        metadata = request.metadata.model_copy(
            update={"tenant_id": tenant_id, "user_id": principal_id}
        )
        try:
            response = await self._runtime_provider().chat.complete(
                request.model_copy(update={"metadata": metadata}),
                prompt=prompt,
            )
            return AppResponse(True, chat_response_data(response))
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "generate chat completion")
