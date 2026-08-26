"""Application-service dependency for chat routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.schemas import AppResponse


class ChatService(Protocol):
    async def create_chat_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse: ...

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool: ...

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse: ...

    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]: ...


def chat_service(request: Request) -> ChatService:
    return cast(ChatService, request.app.state.app_service)


ChatServiceDependency = Annotated[ChatService, Depends(chat_service)]
