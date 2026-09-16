"""Dispatch a validated completion to the requested execution mode."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_runtime.chat import ChatPrompt

from .completion_dependency import CompletionService
from .schemas import CompletionRequest


def chat_options(request: CompletionRequest, principal: Principal) -> ChatExecutionOptions:
    if request.session_id is None:
        raise ValueError("completion session must be resolved before execution")
    return ChatExecutionOptions(
        system=ChatPrompt.DEFAULT,
        graph_search=request.graph_search,
        session_id=request.session_id,
        project_id=request.project_id,
        user_id=principal.user_id,
        model=request.model,
    )


def agent_options(
    request: CompletionRequest, principal: Principal, settings: ApiSettings
) -> AgentExecutionOptions:
    if request.session_id is None:
        raise ValueError("completion session must be resolved before execution")
    return AgentExecutionOptions(
        session_id=request.session_id,
        graph_search=bool(request.graph_search),
        max_steps=request.max_steps,
        deadline_seconds=(
            settings.api_stream_timeout_seconds
            if request.stream
            else settings.api_request_timeout_seconds
        ),
        token_budget=settings.api_agent_token_budget,
        project_id=request.project_id,
        user_id=principal.user_id,
        model=request.model,
    )


async def complete(
    service: CompletionService,
    request: CompletionRequest,
    principal: Principal,
    settings: ApiSettings,
) -> AppResponse:
    if request.mode == "agent":
        return await service.agent_completion(
            request.prompt,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            options=agent_options(request, principal, settings),
        )
    return await service.chat_completion(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=chat_options(request, principal),
    )


def stream(
    service: CompletionService,
    request: CompletionRequest,
    principal: Principal,
    settings: ApiSettings,
) -> AsyncGenerator[dict[str, object], None]:
    if request.mode == "agent":
        return service.agent_stream(
            request.prompt,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            options=agent_options(request, principal, settings),
        )
    return service.chat_stream(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=chat_options(request, principal),
    )
