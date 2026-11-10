"""``POST /v1/chat/completions``: one cited answer, in a session or as a one-off question.

The tenant comes from the credential (and ``X-Tenant-Id``), the session's owner from the
verified user claim. A retry with the same ``Idempotency-Key`` and body replays the
stored answer byte for byte.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.capacity_dependency import ApiCapacityDependency
from harborrag_app.api.dependencies import OperationsDependency, ScopeDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.middleware import current_trace_id
from harborrag_app.operations.contract import OPERATIONS, route_kwargs

from .schemas import ChatCompletionRequest
from .sessions import router as session_router

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
    responses=ERROR_RESPONSES | COMPLETION_RESPONSES,
    **route_kwargs("create_chat_completion"),
    dependencies=[Depends(require_role(OPERATIONS["create_chat_completion"].role, bucket="chat"))],
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
    operations: OperationsDependency,
    scope: ScopeDependency,
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
    completion = await operations.create_chat_completion(
        scope, request, idempotency_key=idempotency_key, trace_id=current_trace_id(http)
    )
    replayed = str(completion.replayed).lower()
    if completion.events is not None:
        return StreamingResponse(
            completion.events,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Idempotency-Replayed": replayed,
            },
        )
    assert completion.body is not None
    return Response(
        content=completion.body.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store", "Idempotency-Replayed": replayed},
    )


router.include_router(session_router)
