"""Authenticated chat completions through the runtime chat façade."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest, MessageRole

from .dependencies import ChatServiceDependency
from .schemas import ChatCompletionRequest, ChatCompletionResponse

router = APIRouter(prefix="/chat", tags=["Chat"])

ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid chat-completion request",
        503: "Chat service unavailable",
    }
)


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    service: ChatServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ChatCompletionResponse:
    authorize_tenant(principal, request.tenant)
    chat_request = HarborChatRequest(
        messages=tuple(
            HarborChatMessage(role=MessageRole(message.role), content=message.content)
            for message in request.messages
        ),
        logical_model=request.model,
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens,
        stop=tuple(request.stop) if isinstance(request.stop, list) else request.stop,
        seed=request.seed,
        sensitive=True,
    )
    response = await service.chat_completion(
        chat_request,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        prompt=request.prompt,
    )
    if not response.ok:
        raise HarborConnectionError("Chat service is unavailable")
    return ChatCompletionResponse.model_validate(response.data)
