"""Authenticated administration of the caller's own long-term memory.

Every owner here is derived from the verified principal (``sub`` and the
configured user-id claim), never from a request field, so a caller cannot ask
about or erase anyone else's memory. The one endpoint that acts on a different
person -- right-to-erasure for a named user -- requires the ``admin`` role and
is still confined to a tenant the caller may access.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_core.ports.memory import MemoryScope

from .dependencies import MemoryAdminServiceDependency
from .schemas import (
    MemoryDeletionResponse,
    MemoryListResponse,
    SessionErasureResponse,
    UserErasureResponse,
)

router = APIRouter(
    prefix="/memory",
    tags=["Memory"],
    dependencies=[Depends(require_api_capacity)],
)

ERROR_RESPONSES = documented_error_responses(
    {
        403: "Tenant access is not permitted, or the role is insufficient",
        404: "Memory, session, or user was not found for this caller",
        422: "Invalid tenant, scope, or identifier",
        503: "Long-term memory is not configured",
    }
)

_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

TenantQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Tenant the memories belong to.",
    ),
]
ProjectQuery = Annotated[
    str | None,
    Query(min_length=1, max_length=128, pattern=_IDENTIFIER),
]
SessionQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER,
        description="Narrow the listing to one of the caller's sessions.",
    ),
]
IdentifierPath = Annotated[str, Path(min_length=1, max_length=128, pattern=_IDENTIFIER)]


def _access(
    principal: Principal,
    tenant: str,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> MemoryAccess:
    """The caller's own isolation key, built only from verified claims."""

    return MemoryAccess(
        tenant_id=tenant,
        principal_id=principal.subject,
        user_id=principal.user_id or principal.subject,
        project_id=project_id,
        session_id=session_id,
    )


@router.get(
    "/memories",
    response_model=MemoryListResponse,
    responses=ERROR_RESPONSES,
)
async def list_memories(  # noqa: PLR0913 - one parameter per documented query filter
    service: MemoryAdminServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: TenantQuery = "DEFAULT",
    project_id: ProjectQuery = None,
    session_id: SessionQuery = None,
    scope: Annotated[MemoryScope | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MemoryListResponse:
    """List the memories visible to the caller, with their provenance."""

    authorize_tenant(principal, tenant)
    access = _access(principal, tenant, project_id=project_id, session_id=session_id)
    result = await service.list_memories(access, scope=scope, limit=limit)
    return MemoryListResponse.model_validate(result.data)


@router.delete(
    "/memories/{memory_id}",
    response_model=MemoryDeletionResponse,
    responses=ERROR_RESPONSES,
)
async def delete_memory(  # noqa: PLR0913 - one parameter per documented query filter
    memory_id: IdentifierPath,
    service: MemoryAdminServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: TenantQuery = "DEFAULT",
    project_id: ProjectQuery = None,
    session_id: SessionQuery = None,
) -> MemoryDeletionResponse:
    """Forget one memory; ``404`` when it is not visible to this caller."""

    authorize_tenant(principal, tenant)
    access = _access(principal, tenant, project_id=project_id, session_id=session_id)
    result = await service.delete_memory(access, memory_id)
    return MemoryDeletionResponse.model_validate(result.data)


@router.delete(
    "/sessions/{session_id}",
    response_model=SessionErasureResponse,
    responses=ERROR_RESPONSES,
)
async def erase_session(
    session_id: IdentifierPath,
    service: MemoryAdminServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: TenantQuery = "DEFAULT",
) -> SessionErasureResponse:
    """Erase one of the caller's sessions: messages, memories, and vectors."""

    authorize_tenant(principal, tenant)
    result = await service.erase_memory_session(_access(principal, tenant, session_id=session_id))
    return SessionErasureResponse.model_validate(result.data)


@router.delete(
    "/users/{user_id}",
    response_model=UserErasureResponse,
    responses=ERROR_RESPONSES,
)
async def erase_user(
    user_id: IdentifierPath,
    service: MemoryAdminServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    tenant: TenantQuery = "DEFAULT",
) -> UserErasureResponse:
    """Right to erasure: remove everything remembered about one end user.

    Sessions are reached through the memories they produced, so the response
    reports exactly what was removed rather than claiming completeness.
    """

    authorize_tenant(principal, tenant)
    result = await service.erase_memory_user(_access(principal, tenant), user_id)
    return UserErasureResponse.model_validate(result.data)
