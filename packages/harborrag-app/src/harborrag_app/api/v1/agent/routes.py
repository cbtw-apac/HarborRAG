"""Authenticated HTTP endpoint for bounded multi-turn agents."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import ApiCapacityDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_core.contracts.errors import HarborConnectionError, HarborNotFoundError

from .dependencies import AgentCompletionService, AgentServiceDependency
from .schemas import (
    AgentCompletionRequest,
    AgentCompletionResponse,
    AgentResumeRequest,
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

_UNAVAILABLE_MESSAGE = "Agent service is unavailable"

RESUME_ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid agent-resume request",
        404: "Agent run not found or not resumable",
        500: "Agent run checkpointing is not configured",
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
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return AgentSessionResponse.model_validate(response.data)


@router.post(
    "/completions",
    response_model=AgentCompletionResponse,
    responses=ERROR_RESPONSES,
)
async def create_agent_completion(
    request: AgentCompletionRequest,
    service: AgentServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    response: Response,
    _capacity: ApiCapacityDependency,
) -> AgentCompletionResponse | StreamingResponse:
    return await _complete_agent(request, service, principal, response)


async def _complete_agent(
    request: AgentCompletionRequest,
    service: AgentCompletionService,
    principal: Principal,
    response: Response,
) -> AgentCompletionResponse | StreamingResponse:
    authorize_tenant(principal, request.tenant)
    response.headers["Cache-Control"] = "no-store"
    if request.stream:
        if not await service.agent_session_exists(
            request.session_id,
            tenant_id=request.tenant,
            principal_id=principal.subject,
        ):
            raise HarborNotFoundError("Conversation session was not found")
        return _stream_response(request, service, principal)
    result = await service.agent_completion(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=_options(request),
    )
    if not result.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return AgentCompletionResponse.model_validate(result.data)


def _stream_response(
    request: AgentCompletionRequest,
    service: AgentCompletionService,
    principal: Principal,
) -> StreamingResponse:
    async def events() -> AsyncIterator[bytes]:
        async for item in service.agent_stream(
            request.prompt,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            options=_options(request),
        ):
            kind = item["kind"]
            if kind == "event":
                event = item["event"]
                payload: object = event
                name = str(event["name"])  # type: ignore[index]
            elif kind == "result":
                payload = item["result"]
                name = "result"
            else:
                payload = {"code": "harbor_connection_error", "message": _UNAVAILABLE_MESSAGE}
                name = "error"
            yield f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()
            if kind in ("result", "error"):
                return

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _options(request: AgentCompletionRequest) -> AgentExecutionOptions:
    return AgentExecutionOptions(
        session_id=request.session_id,
        graph_search=request.graph_search,
        max_steps=request.max_steps,
    )


@router.post(
    "/runs/{run_id}/resume",
    response_model=AgentCompletionResponse,
    responses=RESUME_ERROR_RESPONSES,
)
async def resume_agent_run(
    run_id: str,
    request: AgentResumeRequest,
    service: AgentServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    _capacity: ApiCapacityDependency,
) -> AgentCompletionResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.agent_resume(
        run_id,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=AgentExecutionOptions(
            session_id=request.session_id,
            graph_search=request.graph_search,
            max_steps=request.max_steps,
        ),
    )
    if not response.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return AgentCompletionResponse.model_validate(response.data)
