"""Application-service dependency for agent routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_app.workflow_control.schemas import AppResponse


class AgentCompletionService(Protocol):
    async def create_agent_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse: ...

    async def agent_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool: ...

    async def agent_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse: ...

    def agent_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]: ...

    async def agent_resume(
        self,
        run_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse: ...


def agent_service(request: Request) -> AgentCompletionService:
    return cast(AgentCompletionService, request.app.state.app_service)


AgentServiceDependency = Annotated[AgentCompletionService, Depends(agent_service)]
