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
    ) -> AppResponse:
        return await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
        )

    async def agent_session_exists(
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
