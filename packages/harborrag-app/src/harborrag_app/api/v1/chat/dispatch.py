"""Dispatch a validated completion to the chat service."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from harborrag_app.api.auth.principal import Principal
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_runtime.chat import ChatPrompt

from .completion_dependency import CompletionService
from .schemas import ChatCompletionRequest


def chat_options(request: ChatCompletionRequest, principal: Principal) -> ChatExecutionOptions:
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


async def complete(
    service: CompletionService, request: ChatCompletionRequest, principal: Principal
) -> AppResponse:
    return await service.chat_completion(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=chat_options(request, principal),
    )


def stream(
    service: CompletionService, request: ChatCompletionRequest, principal: Principal
) -> AsyncGenerator[dict[str, object], None]:
    return service.chat_stream(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=chat_options(request, principal),
    )
