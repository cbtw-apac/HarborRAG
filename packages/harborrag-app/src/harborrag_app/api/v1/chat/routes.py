"""``POST /v1/chat/completions``: one cited answer, in a session or as a one-off question.

The tenant comes from the credential (and ``X-Tenant-Id``), the session's owner from the
verified user claim. A retry with the same ``Idempotency-Key`` and body replays the
stored answer byte for byte.
"""

from __future__ import annotations

import contextlib
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Request, Response

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.capacity_dependency import ApiCapacityDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.middleware import current_trace_id
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.chat.completion import (
    Completion,
    RetrievalFound,
    TurnCompleted,
)
from harborrag_core.contracts.errors import HarborConnectionError

from .completion_dependency import CompletionService, CompletionServiceDependency
from .dispatch import chat_options
from .replay import CompletionAttempt
from .schemas import ChatCompletionRequest, ChatCompletionResponse
from .streaming import completion_stream

router = APIRouter(prefix="/chat", tags=["Chat"])

ERROR_RESPONSES = documented_error_responses(
    {
        404: "The session does not exist or is not the caller's (not_found)",
        409: "Another answer is being written in the session or with this Idempotency-Key "
        "(session_busy), nothing is indexed in the scope (no_indexed_content), or the "
        "request with this Idempotency-Key failed (invalid_state)",
        422: "A field fails the schema (validation_error), a project or connection does not "
        "exist (reference_not_found), mode advanced or auto (mode_unavailable), or a "
        "connection outside the scope (connection_out_of_scope)",
        503: "No chat model answered (llm_unavailable), or a store is unavailable",
    }
)
COMPLETION_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "content": {
            "text/event-stream": {
                "schema": {
                    "type": "string",
                    "description": "response.started, one response.output_text.delta with the "
                    "answer, then response.completed with the JSON completion body",
                }
            }
        }
    },
}


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
    responses=ERROR_RESPONSES | COMPLETION_RESPONSES,
    summary="Ask a question",
    description="Answer in mode instant or classic, inside a session or as a one-off question "
    "(leave out session_id). The same Idempotency-Key and body within 24 h returns the "
    "stored answer without a second model call.",
)
async def create_chat_completion(  # noqa: PLR0913 - the route's dependencies
    request: Annotated[
        ChatCompletionRequest,
        Body(
            openapi_examples={
                "question": {
                    "summary": "Ask a one-off question",
                    "description": "Answered over the whole tenant and not stored.",
                    "value": {"message": "What changed in the release policy?", "stream": False},
                },
                "session_question": {
                    "summary": "Ask in a session",
                    "description": "Use a session_id from POST /v1/chat/sessions.",
                    "value": {
                        "session_id": "ses_0b9c1f2e3d4a5b6c7d8e9f0a1b2c3d4e",
                        "message": "Who has Go and Kubernetes experience in Hanoi?",
                        "mode": "classic",
                        "stream": False,
                    },
                },
            }
        ),
    ],
    http: Request,
    service: CompletionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader", bucket="chat"))],
    tenant: TenantDependency,
    _capacity: ApiCapacityDependency,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r".*\S.*",
            description="A retry with the same key and body within 24 h returns the stored result",
        ),
    ] = None,
) -> Response:
    """Answer one question with numbered citations."""

    options = chat_options(request, principal, trace_id=current_trace_id(http))
    attempt = CompletionAttempt.for_request(
        service, request, tenant_id=tenant, principal=principal, key=idempotency_key
    )
    body = await attempt.claim()
    replayed = body is not None
    if body is None:
        completion = await _answer(service, request, tenant, principal, options, attempt)
        body = ChatCompletionResponse.model_validate(completion.data()).model_dump_json()
        await attempt.finish(body, session_id=completion.session_id)
    if request.stream:
        return completion_stream(body, replayed=replayed)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "Idempotency-Replayed": str(replayed).lower()},
    )


async def _answer(  # noqa: PLR0913 - the turn's request, caller and claim
    service: CompletionService,
    request: ChatCompletionRequest,
    tenant: str,
    principal: Principal,
    options: ChatExecutionOptions,
    attempt: CompletionAttempt,
) -> Completion:
    """Run the turn, giving the key back on a failure before any model call and spending it after.

    The turn yields ``RetrievalFound`` after every read and check it makes before its
    first model call, so a failure until then (a store or retrieval outage, a busy
    session, a refused request, a caller that went away) leaves the key reusable. From
    then on a model may have been paid for, and a failure records the key as failed.

    Raises:
        HarborConnectionError: The turn ended without its answer.
    """
    spent = False
    events = service.chat_answer(
        request.message, tenant_id=tenant, principal_id=principal.subject, options=options
    )
    try:
        async with contextlib.aclosing(events):
            async for event in events:
                if isinstance(event, RetrievalFound):
                    spent = True
                elif isinstance(event, TurnCompleted):
                    return event.completion
    except BaseException:
        await (attempt.finish() if spent else attempt.release())
        raise
    await attempt.finish()
    raise HarborConnectionError("The chat turn ended without its answer")
