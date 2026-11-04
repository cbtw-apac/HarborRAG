"""Chat session history: list sessions and read a session's messages.

The owner of a session is assembled from verified tenant and user claims,
never from request fields. A caller cannot widen a listing or read another
user's history by guessing a session ID.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.dependencies import authorize_tenant
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.memory import MemoryAccess

from .dependencies import ConversationServiceDependency
from .schemas import (
    ConversationMessageListResponse,
    SessionListResponse,
)


def _no_store(response: Response) -> None:
    """Keep conversation content out of shared caches."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/sessions", dependencies=[Depends(_no_store)])

ERROR_RESPONSES = documented_error_responses(
    {
        403: "Tenant access is not permitted, or the role is insufficient",
        404: "Conversation was not found for this caller",
        422: "Invalid tenant, identifier, or paging cursor",
        503: "Conversation history is not configured",
    }
)

_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

TenantQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Tenant the conversations belong to.",
    ),
]
CursorQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=512,
        description="Opaque cursor from a previous page's next_cursor.",
    ),
]
AfterQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER,
        description="Return messages after this message id.",
    ),
]
SessionPath = Annotated[str, Path(min_length=1, max_length=128, pattern=_IDENTIFIER)]


def _access(principal: Principal, tenant: str, session_id: str | None = None) -> MemoryAccess:
    """The caller's own conversation key, built only from verified claims."""

    return MemoryAccess(
        tenant_id=tenant,
        principal_id=principal.subject,
        user_id=principal.user_id or principal.subject,
        session_id=session_id,
    )


@router.get(
    "",
    response_model=SessionListResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
    name="list_chat_sessions",
)
async def list_sessions(
    service: ConversationServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader", tenant=False))],
    tenant: TenantQuery = "DEFAULT",
    cursor: CursorQuery = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionListResponse:
    """The caller's chat sessions, newest activity first."""

    authorize_tenant(principal, tenant)
    result = await service.list_conversations(
        _access(principal, tenant), kind="chat", cursor=cursor, limit=limit
    )
    return SessionListResponse.model_validate(
        {
            "sessions": result.data["conversations"],
            "next_cursor": result.data.get("next_cursor"),
        }
    )


@router.get(
    "/{session_id}/messages",
    response_model=ConversationMessageListResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
async def list_conversation_messages(  # noqa: PLR0913 - one parameter per documented query filter
    session_id: SessionPath,
    service: ConversationServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader", tenant=False))],
    tenant: TenantQuery = "DEFAULT",
    after: AfterQuery = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConversationMessageListResponse:
    """One conversation's messages, oldest first; ``404`` when it is not the caller's."""

    authorize_tenant(principal, tenant)
    result = await service.conversation_messages(
        _access(principal, tenant, session_id),
        after=after,
        limit=limit,
    )
    return ConversationMessageListResponse.model_validate(result.data)
