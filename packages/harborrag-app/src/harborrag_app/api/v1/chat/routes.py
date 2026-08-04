"""Authenticated, retrieval-grounded chat completions through the runtime chat façade."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_core.contracts.errors import HarborConnectionError

from .dependencies import ChatServiceDependency
from .schemas import ChatCompletionRequest, ChatCompletionResponse

router = APIRouter(prefix="/chat", tags=["Chat"])

ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid chat-completion request",
        503: "Chat service unavailable",
    }
)

_UNAVAILABLE_MESSAGE = "Chat service is unavailable"


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
    response = await service.chat_completion(
        request.prompt,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        system=request.system,
    )
    if not response.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return ChatCompletionResponse.model_validate(response.data)


@router.post(
    "/stream",
    responses=ERROR_RESPONSES,
)
async def create_chat_completion_stream(
    request: ChatCompletionRequest,
    service: ChatServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> StreamingResponse:
    """Stream one retrieval-grounded chat completion as Server-Sent Events.

    The response always starts as ``200 text/event-stream`` -- once streaming
    begins, HTTP status can no longer change, so failures (including a
    prepare-time failure such as an unreachable retrieval or chat backend)
    surface as an in-band ``event: error`` frame rather than a 503 status.
    """

    authorize_tenant(principal, request.tenant)

    async def events() -> AsyncIterator[bytes]:
        async for event in service.chat_stream(
            request.prompt,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            system=request.system,
        ):
            kind = event["kind"]
            if kind == "citations":
                payload: object = {"citations": event["citations"]}
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

    return StreamingResponse(events(), media_type="text/event-stream")
