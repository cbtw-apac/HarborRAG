"""Authenticated, retrieval-grounded chat completions through the runtime chat façade."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_core.contracts.errors import HarborConnectionError, HarborNotFoundError
from harborrag_runtime.chat import ChatPrompt

from .dependencies import ChatServiceDependency
from .schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatSessionCreateRequest,
    ChatSessionResponse,
)

router = APIRouter(prefix="/chat", tags=["Chat"])

ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid chat-completion request",
        404: "Conversation session not found",
        503: "Chat service unavailable",
    }
)

_UNAVAILABLE_MESSAGE = "Chat service is unavailable"


@router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    responses=ERROR_RESPONSES,
    status_code=201,
)
async def create_chat_session(
    request: ChatSessionCreateRequest,
    service: ChatServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ChatSessionResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.create_chat_session(
        tenant_id=request.tenant,
        principal_id=principal.subject,
    )
    if not response.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return ChatSessionResponse.model_validate(response.data)


@router.get(
    "/completions",
    response_model=ChatCompletionResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
async def create_chat_completion(
    request: Annotated[ChatCompletionRequest, Query()],
    service: ChatServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    response: Response,
) -> ChatCompletionResponse | StreamingResponse:
    authorize_tenant(principal, request.tenant)
    if request.stream:
        if not await service.chat_session_exists(
            request.session_id,
            tenant_id=request.tenant,
            principal_id=principal.subject,
        ):
            raise HarborNotFoundError("Conversation session was not found")
        return _stream_response(request, service, principal)
    # The prompt (up to 65KB) rides in the query string -- GET is the wrong
    # transport for content this sensitive, but until that's migrated to a
    # POST body, at minimum this response must never be cached: caching a
    # non-idempotent GET whose body embeds the prompt/answer would persist
    # sensitive content in an intermediary beyond this request's lifetime.
    response.headers["Cache-Control"] = "no-store"
    result = await service.chat_completion(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        options=_options(request, principal),
    )
    if not result.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return ChatCompletionResponse.model_validate(result.data)


def _stream_response(
    request: ChatCompletionRequest,
    service: ChatServiceDependency,
    principal: Principal,
) -> StreamingResponse:
    async def events() -> AsyncIterator[bytes]:
        async for event in service.chat_stream(
            request.prompt,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            options=_options(request, principal),
        ):
            kind = event["kind"]
            if kind == "citations":
                payload: object = {
                    "citations": event["citations"],
                    "session_id": event["session_id"],
                }
                name = "citations"
            elif kind == "chunk":
                payload = event["chunk"]
                name = str(payload["event"])  # type: ignore[index]
            else:
                payload = {"code": "harbor_connection_error", "message": _UNAVAILABLE_MESSAGE}
                name = "error"
            yield f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()
            if kind == "error":
                return

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _options(request: ChatCompletionRequest, principal: Principal) -> ChatExecutionOptions:
    del principal
    return ChatExecutionOptions(
        system=ChatPrompt.DEFAULT,
        graph_search=request.graph_search,
        session_id=request.session_id,
    )
