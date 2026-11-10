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
from harborrag_app.api.dependencies import OperationsDependency, ScopeDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.operations.contract import OPERATIONS, route_kwargs

from .schemas import ChatSessionCreateRequest, MessageList, Session, SessionList


def _no_store(response: Response) -> None:
    """Keep conversation content out of shared caches."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/sessions", dependencies=[Depends(_no_store)])

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


@router.post(
    "",
    responses=CREATE_ERRORS,
    name="create_chat_session",
    **route_kwargs("create_chat_session"),
    dependencies=[Depends(require_role(OPERATIONS["create_chat_session"].role))],
)
async def create_chat_session(
    request: ChatSessionCreateRequest, operations: OperationsDependency, scope: ScopeDependency
) -> Session:
    """Start a session, scoped to one project of the tenant for its whole life."""
    return await operations.create_chat_session(scope, request)


@router.get(
    "",
    responses=READ_ERRORS,
    name="list_chat_sessions",
    **route_kwargs("list_chat_sessions"),
    dependencies=[Depends(require_role(OPERATIONS["list_chat_sessions"].role))],
)
async def list_sessions(
    operations: OperationsDependency,
    scope: ScopeDependency,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
    q: Annotated[str | None, Query(max_length=200, description="Search titles")] = None,
) -> SessionList:
    """The caller's sessions, newest activity first."""
    return await operations.list_chat_sessions(scope, cursor=cursor, limit=limit, q=q)


@router.get(
    "/{session_id}/messages",
    responses=READ_ERRORS,
    **route_kwargs("list_conversation_messages"),
    dependencies=[Depends(require_role(OPERATIONS["list_conversation_messages"].role))],
)
async def list_conversation_messages(
    session_id: SessionPath,
    operations: OperationsDependency,
    scope: ScopeDependency,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
) -> MessageList:
    """A session's messages with their citations, newest first."""
    return await operations.list_conversation_messages(
        scope, session_id, cursor=cursor, limit=limit
    )
