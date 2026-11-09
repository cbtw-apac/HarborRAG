"""Chat sessions: create one, list the caller's own, and read a session's history.

The owner of a session is assembled from the verified credential and its
tenant, never from request fields. A caller cannot widen a listing or read
another reader's history by guessing a session ID: both answer as if the
session did not exist.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.errors import ReferenceNotFoundError, documented_error_responses
from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_core.contracts.errors import HarborConnectionError, HarborNotFoundError

from .dependencies import ChatServiceDependency, ConversationServiceDependency
from .schemas import ChatSessionCreateRequest, MessageList, Session, SessionList


def _no_store(response: Response) -> None:
    """Keep conversation content out of shared caches."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/sessions", dependencies=[Depends(_no_store)])

Reader = Annotated[Principal, Depends(require_role("reader"))]

CREATE_ERRORS = documented_error_responses(
    {
        403: "Tenant access is not permitted, or the role is insufficient",
        422: "A field fails the schema (validation_error), or project_id is unknown in "
        "the tenant (reference_not_found)",
        503: "The conversation store is unavailable",
    }
)
READ_ERRORS = documented_error_responses(
    {
        403: "Tenant access is not permitted, or the role is insufficient",
        404: "The session does not exist or is not the caller's",
        422: "A malformed or foreign cursor, or a limit out of range",
        503: "The conversation store is unavailable",
    }
)

CursorQuery = Annotated[
    str | None,
    Query(min_length=1, max_length=512, description="next_cursor from the previous page"),
]
LimitQuery = Annotated[int, Query(ge=1, le=100, description="Page size")]
SessionPath = Annotated[
    str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]


def _access(principal: Principal, tenant: str, session_id: str | None = None) -> MemoryAccess:
    """The caller's own conversation key, built only from verified claims."""

    return MemoryAccess(
        tenant_id=tenant,
        principal_id=principal.subject,
        user_id=principal.user_id,
        session_id=session_id,
    )


@router.post(
    "",
    response_model=Session,
    status_code=201,
    responses=CREATE_ERRORS,
    name="create_chat_session",
)
async def create_chat_session(
    request: ChatSessionCreateRequest,
    service: ChatServiceDependency,
    principal: Reader,
    tenant: TenantDependency,
) -> Session:
    """Start a session, scoped to one project of the tenant for its whole life."""

    try:
        result = await service.create_chat_session(
            tenant_id=tenant,
            principal_id=principal.subject,
            user_id=principal.user_id,
            title=request.title,
            project_id=request.project_id,
        )
    except HarborNotFoundError as exc:
        raise ReferenceNotFoundError(str(exc), {"field": "project_id"}) from exc
    if not result.ok:
        raise HarborConnectionError("Chat service is unavailable")
    return Session.model_validate(result.data)


@router.get("", response_model=SessionList, responses=READ_ERRORS, name="list_chat_sessions")
async def list_sessions(  # noqa: PLR0913 - the route's dependencies and query
    service: ConversationServiceDependency,
    principal: Reader,
    tenant: TenantDependency,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
    q: Annotated[str | None, Query(max_length=200, description="Search titles")] = None,
) -> SessionList:
    """The caller's sessions, newest activity first."""

    result = await service.list_conversations(
        _access(principal, tenant), cursor=cursor, limit=limit, title_query=q
    )
    return SessionList.model_validate(result.data)


@router.get("/{session_id}/messages", response_model=MessageList, responses=READ_ERRORS)
async def list_conversation_messages(  # noqa: PLR0913 - the route's dependencies and query
    session_id: SessionPath,
    service: ConversationServiceDependency,
    principal: Reader,
    tenant: TenantDependency,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
) -> MessageList:
    """A session's messages with their citations, newest first."""

    result = await service.conversation_messages(
        _access(principal, tenant, session_id), cursor=cursor, limit=limit
    )
    return MessageList.model_validate(result.data)
