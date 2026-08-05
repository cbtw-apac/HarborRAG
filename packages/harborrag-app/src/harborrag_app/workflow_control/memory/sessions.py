"""Application service for persisted conversation session resources."""

from __future__ import annotations

import secrets
from collections.abc import Sequence

from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_runtime.memory import (
    ConversationIdentity,
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

    async def create(self, *, tenant_id: str, principal_id: str) -> AppResponse:
        session_id = new_session_id()
        await self._repository.create(ConversationIdentity(tenant_id, principal_id, session_id))
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
    ) -> bool:
        return await self._repository.exists(
            ConversationIdentity(tenant_id, principal_id, session_id)
        )


__all__ = ["ConversationSessionService"]
