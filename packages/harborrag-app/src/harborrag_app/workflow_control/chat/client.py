"""Chat use-case forwarding shared by application service implementations."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from harborrag_core.ports.completion_requests import CompletionClaim

from ..memory import ConversationSessionService
from ..schemas import AppResponse
from .completion import TurnEvent
from .options import ChatExecutionOptions
from .service import ChatApplicationService


class ChatClientMixin:
    """Expose chat operations from a composed chat application service."""

    _chat: ChatApplicationService
    _sessions: ConversationSessionService

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        return await self._sessions.claim_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
        )

    async def finish_completion(  # noqa: PLR0913 - mirrors CompletionRequestStore
        self,
        *,
        tenant_id: str,
        user_id: str,
        key: str,
        request_hash: str,
        response_json: str | None,
        session_id: str | None = None,
    ) -> None:
        await self._sessions.finish_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_json=response_json,
            session_id=session_id,
        )

    async def release_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> None:
        await self._sessions.release_completion(
            tenant_id=tenant_id, user_id=user_id, key=key, request_hash=request_hash
        )

    async def create_chat_session(  # noqa: PLR0913 - one component of the new session per argument
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> AppResponse:
        """``user_id`` owns the new conversation and defaults to the principal.

        The transport must pass the same end-user identity it later puts on
        ``ChatExecutionOptions``: the session is keyed by it, so creating as
        the credential and completing as the human would make every turn look
        like an unknown session.

        Raises:
            HarborNotFoundError: ``project_id`` does not exist in ``tenant_id``.
        """

        await self._chat.validate_project(project_id, tenant_id=tenant_id)
        return await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
            user_id=user_id,
            title=title,
            project_id=project_id,
        )

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> bool:
        return await self._sessions.exists(
            session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            user_id=user_id,
        )

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        """Answer one question as the explorer completion body; see ``ChatApplicationService``.

        Raises:
            HarborError: The turn was refused or failed; see ``ChatApplicationService.answer``.
        """
        return await self._chat.complete(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )

    def chat_answer(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[TurnEvent, None]:
        """Run one turn as events; see ``ChatApplicationService.answer``."""
        return self._chat.answer(
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
    ) -> AsyncGenerator[dict[str, object], None]:
        return self._chat.stream(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )


__all__ = ["ChatClientMixin"]
