"""Authenticated completions with shared session, replay, and streaming semantics."""

from __future__ import annotations

from time import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Header, Response
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import ApiCapacityDependency
from harborrag_app.api.dependencies import ResponseContextDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.memory import MemoryAccess
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
    ChatMessageResponse,
    ChatSessionCreateRequest,
    ChatSessionResponse,
    ChatUsageResponse,
    CompletionRequest,
)
from .streaming import stream_response

router = APIRouter(prefix="/chat", tags=["Chat"])

ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid completion request; out-of-scope prompts return a completed refusal",
        404: "Conversation session or project not found",
        409: "No indexed content, busy conversation, or conflicting idempotency key",
        503: "Chat service unavailable",
    }
)
COMPLETION_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "content": {
            "text/event-stream": {
                "schema": {
                    "type": "string",
                    "description": "response.started, retrieval.completed, response.output_text.delta, "
                    "response.citations, response.agent.progress, response.warning, and exactly one "
                    "response.completed or response.error. Scope refusals use response.completed "
                    "with the same payload schema as JSON.",
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
    responses=ERROR_RESPONSES | COMPLETION_RESPONSES,
    summary="Create a retrieval chat completion",
    description="For bounded tool runs use POST /v1/agent/completions. "
    "Omit session_id to create a session. Set stream=true for SSE with the same final "
    "completion payload. Use Idempotency-Key to safely replay a completed request.",
)
async def create_chat_completion(
    request: Annotated[
        ChatCompletionRequest,
        Body(
            openapi_examples={
                "existing_session": {
                    "summary": "Continue a session",
                    "description": "Use the session_id returned by an earlier completion.",
                    "value": {
                        "tenant": "DEFAULT",
                        "session_id": "session-0b9c1f2e3d4a5b6c7d8e9f0a1b2c3d4e",
                        "prompt": "What changed next?",
                        "stream": False,
                    },
                },
                "new_session": {
                    "summary": "Create a session",
                    "value": {
                        "tenant": "DEFAULT",
                        "prompt": "What changed in the release policy?",
                        "stream": False,
                    },
                },
            }
        ),
    ],
    service: CompletionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    context: ResponseContextDependency,
    _capacity: ApiCapacityDependency,
    header_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Stable request key (1–128 characters); must match the body key if both "
            "are supplied. Replays completed requests without another model call.",
        ),
    ] = None,
) -> ChatCompletionResponse | StreamingResponse:
    return await complete_request(request, service, principal, context, header_key)


async def complete_request(
    request: CompletionRequest,
    service: CompletionService,
    principal: Principal,
    context: ResponseContextDependency,
    header_key: str | None,
) -> ChatCompletionResponse | StreamingResponse:
    """Shared admission, replay, and SSE lifecycle for both public surfaces."""

    if header_key is not None:
        if request.idempotency_key is not None and request.idempotency_key != header_key:
            raise HarborValidationError("Body and header idempotency keys must match")
        if not header_key.strip() or len(header_key) > 128:
            raise HarborValidationError("Idempotency-Key must contain 1 to 128 characters")
        request = request.model_copy(update={"idempotency_key": header_key})
    return await _complete_chat(
        request, service, principal, context.response, settings=context.settings
    )


async def _require_session(
    service: CompletionService,
    request: CompletionRequest,
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


async def _validate_request(
    request: CompletionRequest,
    service: CompletionService,
    principal: Principal,
) -> None:
    authorize_tenant(principal, request.tenant)
    if request.mode == "agent":
        await service.validate_agent_model(request.model, tenant_id=request.tenant)
    else:
        await service.validate_chat_model(request.model, tenant_id=request.tenant)
    await service.validate_chat_project(request.project_id, tenant_id=request.tenant)
    if request.session_id is not None:
        await _require_session(service, request, principal, request.session_id)


async def _complete_chat(
    request: CompletionRequest,
    service: CompletionService,
    principal: Principal,
    response: Response,
    *,
    settings: ApiSettings,
) -> ChatCompletionResponse | StreamingResponse:
    await _validate_request(request, service, principal)
    attempt = CompletionAttempt.for_request(service, request, principal)
    replay = await attempt.claim()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotency-Replayed"] = str(replay is not None).lower()
    # A streamed turn returns before this is set: its spend, and the failure it
    # may record, are settled inside the stream rather than here.
    dispatched = False
    try:
        refusal_message = (
            await _classify_refusal(request, service, principal) if replay is None else None
        )
        if replay is not None:
            await _require_session(service, request, principal, replay.session_id)
        elif request.session_id is None:
            created = await service.create_chat_session(
                tenant_id=request.tenant,
                principal_id=principal.subject,
                user_id=principal.user_id,
                # Session lists are scoped to the surface that created them.
                kind="agent" if request.mode == "agent" else "chat",
            )
            if not created.ok:
                raise HarborConnectionError("Chat service is unavailable")
            session = ChatSessionResponse.model_validate(created.data)
            request = request.model_copy(update={"session_id": session.session_id})
        if refusal_message is not None:
            return await _completed_refusal(request, principal, attempt, settings, refusal_message)
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
        # Only a dispatched answer records an unreplayable failure. Admission
        # classification has its own usage record. Everything before dispatch --
        # a busy turn, an unavailable session store, a caller that went away --
        # hands the answer key back instead of burning it.
        if dispatched:
            await attempt.finish()
        else:
            await attempt.release()
        raise


async def _classify_refusal(
    request: CompletionRequest, service: CompletionService, principal: Principal
) -> str | None:
    try:
        await service.validate_completion_scope(
            request.prompt,
            MemoryAccess(
                tenant_id=request.tenant,
                principal_id=principal.subject,
                user_id=principal.user_id,
                project_id=request.project_id,
                session_id=request.session_id,
            ),
            model=request.model,
            mode=request.mode,
        )
    except HarborValidationError as error:
        if error.details.get("reason") != "out_of_scope":
            raise
        return str(error)
    return None


async def _completed_refusal(
    request: CompletionRequest,
    principal: Principal,
    attempt: CompletionAttempt,
    settings: ApiSettings,
    message: str,
) -> ChatCompletionResponse | StreamingResponse:
    refusal = _scope_refusal(request, message)
    await attempt.finish(refusal)
    if request.stream:
        return stream_response(
            request,
            principal,
            settings=settings,
            attempt=attempt,
            replay=refusal,
            replayed=False,
        )
    return refusal


def _scope_refusal(request: CompletionRequest, message: str) -> ChatCompletionResponse:
    """Represent policy admission as a completion without claiming answer-model work."""

    if request.session_id is None:
        raise ValueError("completion session must be resolved before a refusal")
    return ChatCompletionResponse(
        id=f"policy-{uuid4().hex}",
        created=int(time()),
        model="scope_gate",
        provider="policy",
        provider_model="scope_gate",
        message=ChatMessageResponse(role="assistant", content=message),
        outcome="refused",
        refusal_reason="out_of_scope",
        finish_reason="out_of_scope",
        usage=ChatUsageResponse(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        retry_count=0,
        fallback_count=0,
        session_id=request.session_id,
        mode=request.mode,
        project_id=request.project_id,
        memory_persisted=False,
    )
