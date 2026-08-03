"""Application-service dependency for chat routes."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.models.chat import HarborChatRequest
from harborrag_runtime.chat import ChatPrompt


class ChatService(Protocol):
    async def chat_completion(
        self,
        request: HarborChatRequest,
        *,
        tenant_id: str,
        principal_id: str,
        prompt: ChatPrompt | None = None,
    ) -> AppResponse: ...


def chat_service(request: Request) -> ChatService:
    return cast(ChatService, request.app.state.app_service)


ChatServiceDependency = Annotated[ChatService, Depends(chat_service)]
