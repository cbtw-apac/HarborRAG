"""Application service for persisted conversation session resources."""

from __future__ import annotations

from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.completion_requests import CompletionClaim
from harborrag_runtime.memory import (
    ConversationHistoryRepository,
    ConversationIdentity,
    new_session_id,
)

from .presenters import conversation_summary_data


class ConversationSessionService:
    """Create authenticated session resources before a completion is requested."""

    def __init__(self, repository: ConversationHistoryRepository) -> None:
        self._repository = repository

    async def create(  # noqa: PLR0913 - one component of the new session per argument
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> AppResponse:
        """Create a session and return it as the explorer ``Session``.

        Args:
            tenant_id: The tenant the session lives in.
            principal_id: The credential that creates it.
            user_id: The person who owns it; defaults to the shared default user.
            title: Its name; blank leaves it untitled, an over-long one is truncated.
            project_id: The validated project every turn is scoped to, fixed for its life.

        Returns:
            The new session.

        Raises:
            HarborConnectionError: The store did not return the session it just created.
        """

        identity = _identity(tenant_id, principal_id, new_session_id(), user_id)
        await self._repository.create(identity, title=title, project_id=project_id)
        created = await self._repository.get_conversation(identity)
        if created is None:
            raise HarborConnectionError("Conversation store did not keep the new session")
        return AppResponse(True, conversation_summary_data(created))

    async def exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> bool:
        return await self._repository.exists(
            _identity(tenant_id, principal_id, session_id, user_id)
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

    async def release_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> None:
        await self._repository.release_completion(
            tenant_id=tenant_id, user_id=user_id, key=key, request_hash=request_hash
        )


def _identity(
    tenant_id: str,
    principal_id: str,
    session_id: str,
    user_id: str | None,
) -> ConversationIdentity:
    return ConversationIdentity(tenant_id, principal_id, session_id, user_id or DEFAULT_USER)
