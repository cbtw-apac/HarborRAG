"""Failure inbox under ``/v1/admin/failures``.

Handles:
- ``GET`` (editor): page the failed documents with per-category counts
- ``POST /retry`` (editor): retry failures by ID or by filter, one run per root run
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.v1.admin.dependencies import Editor

from .dependencies import IngestionServiceDependency
from .schemas import FailurePage, FailureQuery, FailureRetryAccepted, FailureRetryRequest

router = APIRouter(
    prefix="/admin/failures",
    tags=["Failures"],
    dependencies=[Depends(require_api_capacity)],
)

_ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid failure query, cursor or retry request",
        503: "Ingestion service unavailable",
    }
)


@router.get("", response_model=FailurePage, responses=_ERROR_RESPONSES)
async def list_failures(
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    _principal: Editor,
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
    response_model=FailureRetryAccepted,
    status_code=202,
    responses=_ERROR_RESPONSES,
)
async def retry_failures(
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    principal: Editor,
    body: FailureRetryRequest,
) -> dict[str, Any]:
    """Retry unresolved, retryable failures; answers the new runs, none when nothing matches."""
    return await service.failure_inbox.retry(
        tenant_id=tenant,
        ids=body.ids,
        filter=body.filter.model_dump(exclude_none=True) if body.filter is not None else None,
        actor=principal.subject,
        actor_type=principal.kind,
    )
