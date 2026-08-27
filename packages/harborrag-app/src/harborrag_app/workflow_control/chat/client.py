"""Chat use-case forwarding shared by application service implementations."""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..memory import ConversationSessionService
from ..schemas import AppResponse
from .options import ChatExecutionOptions
from .service import ChatApplicationService


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
        response = await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        if not response.ok:
            return response
        # Publish the greeting in the same envelope a completion uses, so a
        # client renders the opening line through one code path. The agent
        # surface keeps the bare `greeting` string it already published.
        return AppResponse(
            True,
            {
                "session_id": response.data["session_id"],
                "message": {"role": "assistant", "content": response.data["greeting"]},
            },
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
