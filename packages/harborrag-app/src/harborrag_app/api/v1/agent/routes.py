"""Authenticated HTTP endpoint for bounded multi-turn agents."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_core.contracts.errors import HarborConnectionError

from .dependencies import AgentServiceDependency
from .schemas import (
    AgentCompletionRequest,
    AgentCompletionResponse,
    AgentSessionCreateRequest,
    AgentSessionResponse,
)

router = APIRouter(prefix="/agent", tags=["Agent"])

ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid agent-completion request",
        404: "Conversation session not found",
        503: "Agent service unavailable",
    }
)


@router.post(
    "/sessions",
    response_model=AgentSessionResponse,
    responses=ERROR_RESPONSES,
    status_code=201,
)
async def create_agent_session(
    request: AgentSessionCreateRequest,
    service: AgentServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> AgentSessionResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.create_agent_session(
        tenant_id=request.tenant,
        principal_id=principal.subject,
    )
    if not response.ok:
        raise HarborConnectionError("Agent service is unavailable")
    return AgentSessionResponse.model_validate(response.data)


@router.get(
    "/completions",
    response_model=AgentCompletionResponse,
    responses=ERROR_RESPONSES,
)
async def create_agent_completion(
    request: Annotated[AgentCompletionRequest, Query()],
    service: AgentServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> AgentCompletionResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.agent_completion(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=AgentExecutionOptions(
            session_id=request.session_id,
            graph_search=request.graph_search,
            max_steps=request.max_steps,
        ),
    )
    if not response.ok:
        raise HarborConnectionError("Agent service is unavailable")
    return AgentCompletionResponse.model_validate(response.data)
