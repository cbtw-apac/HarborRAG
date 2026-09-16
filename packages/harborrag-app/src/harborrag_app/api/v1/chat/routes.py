"""Authenticated completions with shared session, replay, and streaming semantics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import ApiCapacityDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import (
    HarborConnectionError,
    HarborNotFoundError,
    HarborValidationError,
)

from . import dispatch
from .completion_dependency import CompletionService, CompletionServiceDependency
from .dependencies import ChatServiceDependency
from .replay import CompletionAttempt
from .schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatSessionCreateRequest,
    ChatSessionResponse,
)
from .streaming import stream_response

router = APIRouter(prefix="/chat", tags=["Chat"])

ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid completion request",
        404: "Conversation session or project not found",
        409: "No indexed content, busy conversation, or conflicting idempotency key",
        503: "Chat service unavailable",
    }
)
_COMPLETION_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "content": {
            "text/event-stream": {
                "schema": {
                    "type": "string",
                    "description": "response.started, retrieval.completed, response.output_text.delta, "
                    "response.citations, response.agent.progress, response.warning, and exactly one "
                    "response.completed or response.error. The completed payload matches JSON.",
                }
            }
        }
    },
}


@router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    status_code=201,
    responses=ERROR_RESPONSES,
    deprecated=True,
)
async def create_chat_session(
    request: ChatSessionCreateRequest,
    service: ChatServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ChatSessionResponse:
    authorize_tenant(principal, request.tenant)
    result = await service.create_chat_session(
        tenant_id=request.tenant,
        principal_id=principal.subject,
        user_id=principal.user_id,
    )
    if not result.ok:
        raise HarborConnectionError("Chat service is unavailable")
    return ChatSessionResponse.model_validate(result.data)


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
    responses=ERROR_RESPONSES | _COMPLETION_RESPONSES,
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    service: CompletionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    response: Response,
    http_request: Request,
    _capacity: ApiCapacityDependency,
) -> ChatCompletionResponse | StreamingResponse:
    header_key = http_request.headers.get("Idempotency-Key")
    if header_key is not None:
        if request.idempotency_key is not None and request.idempotency_key != header_key:
            raise HarborValidationError("Body and header idempotency keys must match")
        if not header_key.strip() or len(header_key) > 128:
            raise HarborValidationError("Idempotency-Key must contain 1 to 128 characters")
        request = request.model_copy(update={"idempotency_key": header_key})
    settings: ApiSettings = http_request.app.state.settings
    return await _complete_chat(request, service, principal, response, settings=settings)


async def _require_session(
    service: CompletionService,
    request: ChatCompletionRequest,
    principal: Principal,
    session_id: str,
) -> None:
    if not await service.chat_session_exists(
        session_id,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        user_id=principal.user_id,
    ):
        raise HarborNotFoundError("Conversation session was not found")


async def _complete_chat(
    request: ChatCompletionRequest,
    service: CompletionService,
    principal: Principal,
    response: Response,
    *,
    settings: ApiSettings,
) -> ChatCompletionResponse | StreamingResponse:
    authorize_tenant(principal, request.tenant)
    await service.validate_chat_model(request.model, tenant_id=request.tenant)
    await service.validate_chat_project(request.project_id, tenant_id=request.tenant)
    if request.session_id is not None:
        await _require_session(service, request, principal, request.session_id)
    attempt = CompletionAttempt.for_request(service, request, principal)
    replay = await attempt.claim()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotency-Replayed"] = str(replay is not None).lower()
    # A streamed turn returns before this is set: its spend, and the failure it
    # may record, are settled inside the stream rather than here.
    dispatched = False
    try:
        if replay is not None:
            await _require_session(service, request, principal, replay.session_id)
        elif request.session_id is None:
            created = await service.create_chat_session(
                tenant_id=request.tenant,
                principal_id=principal.subject,
                user_id=principal.user_id,
            )
            if not created.ok:
                raise HarborConnectionError("Chat service is unavailable")
            session = ChatSessionResponse.model_validate(created.data)
            request = request.model_copy(update={"session_id": session.session_id})
        if request.stream:
            return stream_response(
                request, principal, settings=settings, attempt=attempt, replay=replay
            )
        if replay is not None:
            return replay
        dispatched = True
        result = await dispatch.complete(service, request, principal, settings)
        if not result.ok:
            raise HarborConnectionError("Chat service is unavailable")
        payload = ChatCompletionResponse.model_validate({**result.data, "mode": request.mode})
        await attempt.finish(payload)
        return payload
    except BaseException:
        # Only a request that reached the model can have cost anything, and only
        # that one records an unreplayable failure. Everything before dispatch --
        # a busy turn, an unavailable session store, a caller that went away --
        # hands the key back instead of burning it.
        if dispatched:
            await attempt.finish()
        else:
            await attempt.release()
        raise
