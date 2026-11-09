"""Application-service dependency for chat routes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.chat.completion import TurnEvent
from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_app.workflow_control.schemas import AppResponse


class ChatService(Protocol):
    async def create_chat_session(  # noqa: PLR0913 - one component of the new session per argument
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> AppResponse: ...

    def chat_answer(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[TurnEvent, None]: ...


class ConversationService(Protocol):
    """The caller's own conversation directory, as the routes consume it."""

    async def list_conversations(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
        title_query: str | None = None,
    ) -> AppResponse: ...

    async def conversation_messages(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> AppResponse: ...


def chat_service(request: Request) -> ChatService:
    return cast(ChatService, request.app.state.app_service)


def conversation_service(request: Request) -> ConversationService:
    return cast(ConversationService, request.app.state.app_service)


ChatServiceDependency = Annotated[ChatService, Depends(chat_service)]
ConversationServiceDependency = Annotated[ConversationService, Depends(conversation_service)]
