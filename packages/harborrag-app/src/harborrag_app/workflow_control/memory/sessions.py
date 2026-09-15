"""Application service for persisted conversation session resources."""

from __future__ import annotations

import secrets
from collections.abc import Sequence

from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.completion_requests import CompletionClaim
from harborrag_runtime.memory import (
    ConversationIdentity,
    ConversationKind,
    ConversationRepository,
    new_session_id,
)

_GREETINGS: tuple[str, ...] = (
    "Hello! How can I help you today?",
    "Hi! What would you like to explore?",
    "Welcome! Ask me anything about your indexed knowledge.",
)


class ConversationSessionService:
    """Create authenticated session resources before a completion is requested."""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        greetings: Sequence[str] = _GREETINGS,
    ) -> None:
        if not greetings:
            raise ValueError("conversation greetings must not be empty")
        self._repository = repository
        self._greetings = tuple(greetings)

    async def create(  # noqa: PLR0913 - one component of the new session per argument
        self,
        *,
        tenant_id: str,
        principal_id: str,
        kind: ConversationKind = "chat",
        user_id: str | None = None,
        title: str | None = None,
    ) -> AppResponse:
        """``user_id`` owns the conversation; it defaults to the principal.

        ``title`` names the conversation at creation so a client does not have
        to create then immediately rename; blank leaves it unnamed, and an
        over-long title is truncated by the port rather than rejected.
        """

        session_id = new_session_id()
        await self._repository.create(
            _identity(tenant_id, principal_id, session_id, user_id),
            kind=kind,
            title=title,
        )
        return AppResponse(
            True,
            {
                "session_id": session_id,
                "greeting": secrets.choice(self._greetings),
            },
        )

    async def exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        kind: ConversationKind | None = None,
        user_id: str | None = None,
    ) -> bool:
        """``kind`` restricts the match to sessions created by that surface."""

        return await self._repository.exists(
            _identity(tenant_id, principal_id, session_id, user_id),
            kind=kind,
        )

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        return await self._repository.claim_completion(
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
        await self._repository.finish_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_json=response_json,
            session_id=session_id,
        )


def _identity(
    tenant_id: str,
    principal_id: str,
    session_id: str,
    user_id: str | None,
) -> ConversationIdentity:
    return ConversationIdentity(tenant_id, principal_id, session_id, user_id or DEFAULT_USER)


__all__ = ["ConversationSessionService"]
