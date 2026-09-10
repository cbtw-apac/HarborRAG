"""Authenticated, retrieval-grounded chat completions through the runtime chat façade."""

from __future__ import annotations

import contextlib
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
        404: "Conversation session or project not found",
        409: "Nothing has been ingested for this tenant yet",
        503: "Chat service unavailable",
    }
)

_UNAVAILABLE_MESSAGE = "Chat service is unavailable"
_STREAM_DEADLINE_MESSAGE = "Chat stream exceeded its server deadline"
_MEMORY_WARNING = {
    "code": "conversation_memory_unavailable",
    "message": "The answer was generated but could not be saved to conversation memory",
}
# ``stream: true`` answers on the same route with Server-Sent Events, so the
# schema has to carry both media types: a generated client that sees only the
# completion model has no idea the streaming shape exists. Only the extra
# media type is declared here -- FastAPI still contributes the generated
# ``application/json`` entry for ``ChatCompletionResponse``.
_COMPLETION_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "content": {
            "text/event-stream": {
                "schema": {
                    "type": "string",
                    "title": "Chat completion event stream",
                    "description": (
                        "Present only when the request set `stream: true`. One `citations` "
                        "frame, then one frame per model chunk named for its event "
                        "(`text_delta`, `completed`, ...), optionally a `warning` frame, "
                        "and at most one terminal `error` frame."
                    ),
                }
            }
        },
        "description": "Chat completion, as JSON or as an event stream",
    }
}

_GENERIC_STREAM_ERROR = {"code": "harbor_connection_error", "message": _UNAVAILABLE_MESSAGE}
# Once the status line is sent, the only way to tell a caller *what* went wrong
# is the terminal frame, and every failure reading "the chat service is
# unavailable" sends a retry at a request that will never succeed. This is an
# allowlist rather than a projection of the application service's error type:
# adding a row means accepting that name and message as public, so a new
# internal exception cannot start describing itself to callers on its own.
_STREAM_ERRORS: dict[str, dict[str, str]] = {
    "HarborNotFoundError": {
        "code": "harbor_not_found_error",
        "message": "Conversation session or project was not found",
    },
    "HarborValidationError": {
        "code": "harbor_validation_error",
        "message": "The chat request was rejected",
    },
    "HarborNoIndexedContentError": {
        "code": "no_indexed_content",
        "message": "No content has been ingested yet, so there is nothing to search",
    },
    "ChatStreamError": {
        "code": "chat_stream_error",
        "message": "The chat provider stream failed",
    },
}


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
        user_id=principal.user_id,
        title=request.title,
    )
    if not response.ok:
        raise HarborConnectionError(_UNAVAILABLE_MESSAGE)
    return ChatSessionResponse.model_validate(response.data)


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES | _COMPLETION_RESPONSES,
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    service: ChatServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    response: Response,
    http_request: Request,
    _capacity: ApiCapacityDependency,
) -> ChatCompletionResponse | StreamingResponse:
    settings: ApiSettings = http_request.app.state.settings
    return await _complete_chat(request, service, principal, response, settings=settings)


async def _complete_chat(
    request: ChatCompletionRequest,
    service: ChatServiceDependency,
    principal: Principal,
    response: Response,
    *,
    settings: ApiSettings,
) -> ChatCompletionResponse | StreamingResponse:
    authorize_tenant(principal, request.tenant)
    # Before the stream/JSON branch on purpose: a disallowed model must be one
    # 422 either way, never an error frame after the headers are on the wire.
    await service.validate_chat_model(request.model, tenant_id=request.tenant)
    if request.stream:
        # The scope checks the application service makes inside a turn cannot
        # reach a stream's caller as a status code, so the stream branch asks
        # for both up front. The JSON branch does not repeat them: there they
        # run before anything is written and surface as a real 404 already.
        if not await service.chat_session_exists(
            request.session_id,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            user_id=principal.user_id,
        ):
            raise HarborNotFoundError("Conversation session was not found")
        await service.validate_chat_project(request.project_id, tenant_id=request.tenant)
        # ``_stream_response`` sets its own no-store header: FastAPI does not
        # merge the injected ``Response``'s headers into a ``Response`` the
        # handler returns itself, so setting it here would be dead code.
        return _stream_response(
            request,
            service,
            principal,
            timeout_seconds=settings.api_stream_timeout_seconds,
        )
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
    *,
    timeout_seconds: float,
) -> StreamingResponse:
    async def events() -> AsyncGenerator[bytes, None]:
        # ``aclosing`` and not a bare ``async for``: when this generator is
        # closed -- the stream deadline firing, or the client hanging up -- the
        # service stream must be closed with it, inline. That is what lets it
        # persist the text it already delivered while it still holds the
        # session lock. An abandoned ``async for`` defers the same work to
        # asyncgen finalization, which runs later and in no particular order.
        stream = service.chat_stream(
            request.prompt,
            tenant_id=request.tenant,
            principal_id=principal.subject,
            options=_options(request, principal),
        )
        async with contextlib.aclosing(stream):
            async for event in stream:
                kind = event["kind"]
                if kind == "citations":
                    payload: object = {
                        "citations": event["citations"],
                        "session_id": event["session_id"],
                        "project_id": event.get("project_id"),
                    }
                    name = "citations"
                elif kind == "chunk":
                    payload = event["chunk"]
                    name = str(payload["event"])  # type: ignore[index]
                elif kind == "cited_sources":
                    payload = {
                        "citations": event["citations"],
                        "session_id": event["session_id"],
                        "project_id": event.get("project_id"),
                    }
                    name = "cited_sources"
                elif kind == "warning":
                    payload = _MEMORY_WARNING
                    name = "warning"
                else:
                    payload = _STREAM_ERRORS.get(
                        str(event.get("error_type", "")), _GENERIC_STREAM_ERROR
                    )
                    name = "error"
                yield sse_frame(name, payload)
                if kind == "error":
                    return

    return StreamingResponse(
        bounded_sse_frames(
            events(),
            timeout_seconds=timeout_seconds,
            error_message=_STREAM_DEADLINE_MESSAGE,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _options(request: ChatCompletionRequest, principal: Principal) -> ChatExecutionOptions:
    return ChatExecutionOptions(
        system=ChatPrompt.DEFAULT,
        graph_search=request.graph_search,
        session_id=request.session_id,
        project_id=request.project_id,
        user_id=principal.user_id,
        model=request.model,
    )
