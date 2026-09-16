"""Authenticated HTTP endpoint for bounded multi-turn agents."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import ApiCapacityDependency
from harborrag_app.api.dependencies import ResponseContextDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.chat.completion_dependency import CompletionServiceDependency
from harborrag_app.api.v1.chat.routes import COMPLETION_RESPONSES, complete_request
from harborrag_app.api.v1.chat.sessions import session_router
from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_core.contracts.errors import HarborConnectionError

from .dependencies import AgentServiceDependency
from .schemas import (
    AgentCompletionRequest,
    AgentCompletionResponse,
    AgentResumeRequest,
    AgentSessionCreateRequest,
    AgentSessionResponse,
)

router = APIRouter(prefix="/agent", tags=["Agent"])
router.include_router(session_router("agent"))

ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid agent-completion request",
        404: "Conversation session or project not found",
        409: "No indexed content, busy session, or conflicting idempotency key",
        503: "Agent service unavailable",
    }
)

_UNAVAILABLE_MESSAGE = "Agent service is unavailable"

RESUME_ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid agent-resume request",
        404: "Agent run not found or not resumable",
        409: "Agent run already has an active executor",
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
    responses=ERROR_RESPONSES | COMPLETION_RESPONSES,
    summary="Create a bounded agent completion",
    description="Omit session_id to create an agent session. Set stream=true for the same SSE "
    "event contract as chat. Use Idempotency-Key to replay completed requests safely.",
)
async def create_agent_completion(
    request: AgentCompletionRequest,
    service: CompletionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    context: ResponseContextDependency,
    _capacity: ApiCapacityDependency,
    header_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="Stable request key (1–128 characters)."),
    ] = None,
) -> AgentCompletionResponse | StreamingResponse:
    result = await complete_request(request, service, principal, context, header_key)
    if isinstance(result, StreamingResponse):
        return result
    return AgentCompletionResponse.model_validate(result.model_dump())


def _options(
    request: AgentResumeRequest,
    principal: Principal,
    *,
    settings: ApiSettings,
) -> AgentExecutionOptions:
    """Resume under the server-owned JSON deadline and total token budget."""

    return AgentExecutionOptions(
        session_id=request.session_id,
        graph_search=request.graph_search,
        max_steps=request.max_steps,
        deadline_seconds=settings.api_request_timeout_seconds,
        token_budget=settings.api_agent_token_budget,
        project_id=request.project_id,
        user_id=principal.user_id,
        # Resume has no model of its own: a run keeps whatever it started
        # under, so its checkpoint is never continued on a different model.
        model=None,
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
    context: ResponseContextDependency,
    _capacity: ApiCapacityDependency,
) -> AgentCompletionResponse:
    authorize_tenant(principal, request.tenant)
    result = await service.agent_resume(
        run_id,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=_options(request, principal, settings=context.settings),
    )
    if not result.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    context.response.headers["Cache-Control"] = "no-store"
    return AgentCompletionResponse.model_validate(result.data)
