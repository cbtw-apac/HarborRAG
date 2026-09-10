"""The authenticated caller's own conversations: list, read, rename, delete.

The owner of a conversation is always assembled from verified claims
(``sub`` plus the configured user-id claim) and never from a request field, so
no query parameter or body key can widen a listing to someone else's history.
``user_id`` is what owns a conversation, which is what keeps one shared
service credential from pooling several people's conversations together.

Deleting a conversation goes through the same erasure the memory surface
exposes (``DELETE /v1/memory/sessions/{session_id}``), so the memories and
vector points the conversation produced go with it instead of outliving the
messages they were extracted from.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.v1.memory.schemas import SessionErasureResponse
from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_core.ports.conversation import ConversationKind

from .dependencies import ConversationServiceDependency
from .schemas import (
    ConversationListResponse,
    ConversationMessageListResponse,
    ConversationRenameRequest,
    ConversationRenameResponse,
)

router = APIRouter(prefix="/chat/conversations", tags=["Chat"])

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
KindQuery = Annotated[
    ConversationKind | None,
    Query(description="Restrict the listing to the chat or the agent surface."),
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
    response_model=ConversationListResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
async def list_conversations(  # noqa: PLR0913 - one parameter per documented query filter
    service: ConversationServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: TenantQuery = "DEFAULT",
    kind: KindQuery = None,
    cursor: CursorQuery = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ConversationListResponse:
    """The caller's own conversations, newest activity first."""

    authorize_tenant(principal, tenant)
    result = await service.list_conversations(
        _access(principal, tenant),
        kind=kind,
        cursor=cursor,
        limit=limit,
    )
    return ConversationListResponse.model_validate(result.data)


@router.get(
    "/{session_id}/messages",
    response_model=ConversationMessageListResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
async def list_conversation_messages(  # noqa: PLR0913 - one parameter per documented query filter
    session_id: SessionPath,
    service: ConversationServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
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


@router.patch(
    "/{session_id}",
    response_model=ConversationRenameResponse,
    responses=ERROR_RESPONSES,
)
async def rename_conversation(
    session_id: SessionPath,
    request: ConversationRenameRequest,
    service: ConversationServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: TenantQuery = "DEFAULT",
) -> ConversationRenameResponse:
    """Retitle the caller's conversation; an empty title clears it."""

    authorize_tenant(principal, tenant)
    result = await service.rename_conversation(
        _access(principal, tenant, session_id),
        title=request.title,
    )
    return ConversationRenameResponse.model_validate(result.data)


@router.delete(
    "/{session_id}",
    response_model=SessionErasureResponse,
    responses=ERROR_RESPONSES,
)
async def delete_conversation(
    session_id: SessionPath,
    service: ConversationServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: TenantQuery = "DEFAULT",
) -> SessionErasureResponse:
    """Erase the conversation's messages, memories, and vector points."""

    authorize_tenant(principal, tenant)
    result = await service.delete_conversation(_access(principal, tenant, session_id))
    return SessionErasureResponse.model_validate(result.data)
