"""Authenticated HTTP endpoint for bounded multi-turn agents."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import ApiCapacityDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.sse import bounded_sse_frames, sse_frame
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
        404: "Conversation session or project not found",
        503: "Agent service unavailable",
    }
)

_UNAVAILABLE_MESSAGE = "Agent service is unavailable"
_STREAM_DEADLINE_MESSAGE = "Agent stream exceeded its server deadline"

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
        user_id=principal.user_id,
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
    http_request: Request,
    _capacity: ApiCapacityDependency,
) -> AgentCompletionResponse | StreamingResponse:
    settings: ApiSettings = http_request.app.state.settings
    return await _complete_agent(request, service, principal, response, settings=settings)


async def _complete_agent(
    request: AgentCompletionRequest,
    service: AgentCompletionService,
    principal: Principal,
    response: Response,
    *,
    settings: ApiSettings,
) -> AgentCompletionResponse | StreamingResponse:
    authorize_tenant(principal, request.tenant)
    response.headers["Cache-Control"] = "no-store"
    # Before the stream/JSON branch on purpose: a disallowed model must be one
    # 422 either way, never an error frame after the headers are on the wire.
    await service.validate_agent_model(request.model, tenant_id=request.tenant)
    if request.stream:
        if not await service.agent_session_exists(
            request.session_id,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            user_id=principal.user_id,
        ):
            raise HarborNotFoundError("Conversation session was not found")
        return _stream_response(request, service, principal, settings=settings)
    result = await service.agent_completion(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=_options(request, principal, settings=settings, stream=False),
    )
    if not result.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return AgentCompletionResponse.model_validate(result.data)


def _stream_response(
    request: AgentCompletionRequest,
    service: AgentCompletionService,
    principal: Principal,
    *,
    settings: ApiSettings,
) -> StreamingResponse:
    async def events() -> AsyncGenerator[bytes, None]:
        async for item in service.agent_stream(
            request.prompt,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            options=_options(request, principal, settings=settings, stream=True),
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
            yield sse_frame(name, payload)
            if kind in ("result", "error"):
                return

    return StreamingResponse(
        bounded_sse_frames(
            events(),
            timeout_seconds=settings.api_stream_timeout_seconds,
            error_message=_STREAM_DEADLINE_MESSAGE,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _options(
    request: AgentCompletionRequest | AgentResumeRequest,
    principal: Principal,
    *,
    settings: ApiSettings,
    stream: bool,
) -> AgentExecutionOptions:
    """Hand the server-owned budgets to the agent: the applicable HTTP deadline
    (request timeout for JSON responses, stream timeout for SSE) and the total
    token budget. The application service derives the engine's run timeout
    from that deadline so a graceful ``timeout`` stop stays reachable.
    """

    return AgentExecutionOptions(
        session_id=request.session_id,
        graph_search=request.graph_search,
        max_steps=request.max_steps,
        deadline_seconds=(
            settings.api_stream_timeout_seconds if stream else settings.api_request_timeout_seconds
        ),
        token_budget=settings.api_agent_token_budget,
        project_id=request.project_id,
        user_id=principal.user_id,
        # Resume has no model of its own: a run keeps whatever it started
        # under, so its checkpoint is never continued on a different model.
        model=request.model if isinstance(request, AgentCompletionRequest) else None,
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
    http_request: Request,
    _capacity: ApiCapacityDependency,
) -> AgentCompletionResponse:
    authorize_tenant(principal, request.tenant)
    settings: ApiSettings = http_request.app.state.settings
    response = await service.agent_resume(
        run_id,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=_options(request, principal, settings=settings, stream=False),
    )
    if not response.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return AgentCompletionResponse.model_validate(response.data)
