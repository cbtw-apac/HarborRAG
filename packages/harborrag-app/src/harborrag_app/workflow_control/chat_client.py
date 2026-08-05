"""Chat use-case forwarding shared by application service implementations."""

from __future__ import annotations

from collections.abc import AsyncIterator

from .chat import ChatApplicationService, ChatExecutionOptions
from .memory import ConversationSessionService
from .schemas import AppResponse


class ChatClientMixin:
    """Expose chat operations from a composed chat application service."""

    _chat: ChatApplicationService
    _sessions: ConversationSessionService

    async def create_chat_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
        )

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool:
        return await self._sessions.exists(
            session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        return await self._chat.complete(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )

    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]:
        return self._chat.stream(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )


__all__ = ["ChatClientMixin"]
