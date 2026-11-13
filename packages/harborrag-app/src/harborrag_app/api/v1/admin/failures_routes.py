"""`/v1/admin/failures`: currently-failing documents of one connection.

Handles:
- ``GET /v1/admin/failures?connection={project_id}/{connection_id}`` (reader):
  documents stuck failing, with their attempt count
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.workflow_control.control_plane.settings import parse_level

from .dependencies import Administration, error_responses
from .failures_schemas import DocumentFailureOut, DocumentFailurePage

failures_router = APIRouter(
    prefix="/admin",
    tags=["Failures"],
    dependencies=[Depends(require_role("reader"))],
)

ConnectionParam = Annotated[
    str,
    Query(
        alias="connection",
        description="{project_id}/{connection_id}",
        examples=["cpm/cpm-tickets"],
    ),
]


@failures_router.get(
    "/failures",
    summary="List a connection's failing documents",
    response_model=DocumentFailurePage,
    responses=error_responses(422),
)
async def list_document_failures(
    service: Administration,
    tenant: TenantDependency,
    connection: ConnectionParam,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> DocumentFailurePage:
    """Documents currently stuck failing for one connection, newest failure first."""
    _, scope_id = parse_level(f"connection:{connection}")
    failures = await service.failures_service.list_failures(
        tenant_id=tenant, connection_id=scope_id, limit=limit
    )
    return DocumentFailurePage(
        items=[
            DocumentFailureOut(
                document_id=str(failure.document_id),
                document_version_id=str(failure.document_version_id),
                failed_stage=failure.failed_stage,
                category=failure.category.value,
                retryable=failure.retryable,
                safe_error_code=failure.safe_error_code,
                attempts=failure.attempt_count,
                first_failure_at=failure.first_failure_at,
                last_failure_at=failure.last_failure_at,
            )
            for failure in failures
        ]
    )


routers = (failures_router,)
