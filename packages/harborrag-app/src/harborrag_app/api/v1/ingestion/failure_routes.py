"""Failure inbox under ``/v1/admin/failures``.

Handles:
- ``GET`` (editor): page the failed documents with per-category counts
- ``POST /retry`` (editor): retry failures by ID or by filter, one run per root run
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.dependencies import OperationsDependency, ScopeDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.operations.contract import OPERATIONS, route_kwargs

from .schemas import FailurePage, FailureQuery, FailureRetryAccepted, FailureRetryRequest

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
    dependencies=[Depends(require_role(OPERATIONS["list_failures"].role))],
)
async def list_failures(
    operations: OperationsDependency,
    scope: ScopeDependency,
    query: Annotated[FailureQuery, Query()],
) -> FailurePage:
    """Newest-first page of the tenant's failed documents."""
    return await operations.list_failures(scope, query)


@router.post(
    "/retry",
    responses=_ERROR_RESPONSES,
    **route_kwargs("retry_failures"),
    dependencies=[Depends(require_role(OPERATIONS["retry_failures"].role))],
)
async def retry_failures(
    operations: OperationsDependency, scope: ScopeDependency, body: FailureRetryRequest
) -> FailureRetryAccepted:
    """Retry unresolved, retryable failures; answers the new runs, 409 when nothing matches."""
    return await operations.retry_failures(scope, body)
