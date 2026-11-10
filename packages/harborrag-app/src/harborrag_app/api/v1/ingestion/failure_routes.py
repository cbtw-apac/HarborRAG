"""Failure inbox under ``/v1/admin/failures``.

Handles:
- ``GET`` (editor): page the failed documents with per-category counts
- ``POST /retry`` (editor): retry failures by ID or by filter, one run per root run
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.operations.contract import OPERATIONS, route_kwargs

from .dependencies import IngestionServiceDependency
from .schemas import FailureQuery, FailureRetryRequest

router = APIRouter(
    prefix="/admin/failures",
    tags=["Failures"],
    dependencies=[Depends(require_api_capacity)],
)

_ERROR_RESPONSES = documented_error_responses(
    {
        409: "No retryable failure matches the retry request",
        422: "Invalid failure query, cursor or retry request",
        503: "Ingestion service unavailable",
    }
)


@router.get(
    "",
    responses=_ERROR_RESPONSES,
    **route_kwargs("list_failures"),
)
async def list_failures(
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    _principal: Annotated[Principal, Depends(require_role(OPERATIONS["list_failures"].role))],
    query: Annotated[FailureQuery, Query()],
) -> dict[str, Any]:
    """Newest-first page of the tenant's failed documents."""
    return await service.failure_inbox.page(
        tenant_id=tenant,
        project_id=query.project_id,
        connection_ref=query.connection_ref,
        stage=query.stage,
        error_category=query.error_category,
        resolved=query.resolved,
        q=query.q,
        cursor=query.cursor,
        limit=query.limit,
        include_total=query.include_total,
    )


@router.post(
    "/retry",
    responses=_ERROR_RESPONSES,
    **route_kwargs("retry_failures"),
)
async def retry_failures(
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    principal: Annotated[Principal, Depends(require_role(OPERATIONS["retry_failures"].role))],
    body: FailureRetryRequest,
) -> dict[str, Any]:
    """Retry unresolved, retryable failures; answers the new runs, 409 when nothing matches."""
    return await service.failure_inbox.retry(
        tenant_id=tenant,
        ids=body.ids,
        filter=body.filter.model_dump(exclude_none=True) if body.filter is not None else None,
        actor=principal.subject,
        actor_type=principal.kind,
    )
