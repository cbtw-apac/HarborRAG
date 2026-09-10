"""Chat use-case forwarding shared by application service implementations."""

from __future__ import annotations

from collections.abc import AsyncGenerator

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
        user_id: str | None = None,
        title: str | None = None,
    ) -> AppResponse:
        """``user_id`` owns the new conversation and defaults to the principal.

        The transport must pass the same end-user identity it later puts on
        ``ChatExecutionOptions``: the session is keyed by it, so creating as
        the credential and completing as the human would make every turn look
        like an unknown session.
        """

        return await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
            kind="chat",
            user_id=user_id,
            title=title,
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
            kind="chat",
            user_id=user_id,
        )

    async def validate_chat_model(self, model: str | None, *, tenant_id: str) -> None:
        """Raise ``HarborValidationError`` unless this tenant may use ``model``.

        Called by the transport before it commits to a turn -- including
        before it opens a stream -- so a disallowed name is one status code
        rather than a mid-body error frame.
        """

        await self._chat.validate_model(model, tenant_id=tenant_id)

    async def validate_chat_project(self, project_id: str | None, *, tenant_id: str) -> None:
        """Raise ``HarborNotFoundError`` unless ``project_id`` exists in ``tenant_id``.

        Called by the transport before it opens a stream, for the same reason
        as ``validate_chat_model``: a scope error must be a status code, not a
        frame in an otherwise successful response body.
        """

        await self._chat.validate_project(project_id, tenant_id=tenant_id)

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
    ) -> AsyncGenerator[dict[str, object], None]:
        return self._chat.stream(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )


__all__ = ["ChatClientMixin"]
