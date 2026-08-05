"""Agent portion of the application-service public facade."""

from __future__ import annotations

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
    ) -> AppResponse:
        return await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
        )

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


__all__ = ["AgentClientMixin"]
