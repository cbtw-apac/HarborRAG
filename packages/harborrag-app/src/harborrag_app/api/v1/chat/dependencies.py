"""Application-service dependency for chat routes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.ports.conversation import ConversationKind


class ChatService(Protocol):
    async def create_chat_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
    ) -> AppResponse: ...

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> bool: ...

    async def validate_chat_model(self, model: str | None, *, tenant_id: str) -> None: ...

    async def validate_chat_project(self, project_id: str | None, *, tenant_id: str) -> None: ...

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse: ...

    # ``AsyncGenerator`` and not ``AsyncIterator``: the transport closes this
    # stream when the client goes away, and the service persists the text it
    # already delivered while handling that close. ``aclose`` is part of the
    # contract, not an implementation detail.
    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[dict[str, object], None]: ...


class ConversationService(Protocol):
    """The caller's own conversation directory, as the routes consume it."""

    async def list_conversations(
        self,
        access: MemoryAccess,
        *,
        kind: ConversationKind | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> AppResponse: ...

    async def conversation_messages(
        self,
        access: MemoryAccess,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> AppResponse: ...

    async def rename_conversation(self, access: MemoryAccess, *, title: str) -> AppResponse: ...

    async def delete_conversation(self, access: MemoryAccess) -> AppResponse: ...


def chat_service(request: Request) -> ChatService:
    return cast(ChatService, request.app.state.app_service)


def conversation_service(request: Request) -> ConversationService:
    return cast(ConversationService, request.app.state.app_service)


ChatServiceDependency = Annotated[ChatService, Depends(chat_service)]
ConversationServiceDependency = Annotated[ConversationService, Depends(conversation_service)]
