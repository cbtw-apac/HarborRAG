"""Agent portion of the application-service public facade."""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..memory import ConversationSessionService
from ..schemas import AppResponse
from .options import AgentExecutionOptions
from .service import AgentApplicationService


class AgentClientMixin:
    _agent: AgentApplicationService
    _sessions: ConversationSessionService

    async def create_agent_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> AppResponse:
        """``user_id`` owns the new conversation and defaults to the principal."""

        return await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
            kind="agent",
            user_id=user_id,
        )

    async def agent_session_exists(
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
            kind="agent",
            user_id=user_id,
        )

    async def validate_agent_model(self, model: str | None, *, tenant_id: str) -> None:
        """Raise ``HarborValidationError`` unless this tenant may use ``model``."""

        await self._agent.validate_model(model, tenant_id=tenant_id)

    async def agent_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        return await self._agent.complete(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )

    def agent_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]:
        return self._agent.stream(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )

    async def agent_resume(
        self,
        run_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        return await self._agent.resume(
            run_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )


__all__ = ["AgentClientMixin"]
