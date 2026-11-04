"""Ingestion runs.

Handles:
- ``POST /v1/admin/ingestions`` (editor): start an ingestion of one connection

Does NOT read or control runs yet; ``GET /v1/admin/ingestions[/{ingestion_id}]`` and the
pause, resume, cancel and retry actions join this router.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.errors import ReferenceNotFoundError
from harborrag_app.workflow_control.control_plane.connections import ConnectionKey
from harborrag_core.contracts.errors import HarborNotFoundError

from .connection_schemas import IngestionOut, IngestionStartRequest
from .dependencies import Administration, Editor, error_responses

router = APIRouter(
    prefix="/admin/ingestions",
    tags=["Ingestions"],
    dependencies=[Depends(require_role("editor"))],
)


@router.post(
    "",
    summary="Start an ingestion",
    response_model=IngestionOut,
    status_code=202,
    responses=error_responses(409, 422, 501),
)
async def start_ingestion(
    service: Administration,
    tenant: TenantDependency,
    principal: Editor,
    body: IngestionStartRequest,
    response: Response,
) -> dict[str, Any]:
    """Start an incremental or forced ingestion of a connection; 409 while one is running."""
    try:
        await service.project_service.get(tenant_id=tenant, project_id=body.project_id)
    except HarborNotFoundError as exc:
        raise ReferenceNotFoundError(str(exc), {"field": "project_id"}) from exc
    try:
        ingestion = await service.connection_service.sync(
            ConnectionKey(tenant, body.project_id, body.connection_id),
            actor=principal.subject,
            mode=body.mode,
            item_refs=tuple(body.item_refs),
            updated_after=body.updated_after.isoformat() if body.updated_after else None,
        )
    except HarborNotFoundError as exc:
        raise ReferenceNotFoundError(str(exc), {"field": "connection_id"}) from exc
    response.headers["Location"] = f"/v1/admin/ingestions/{ingestion['id']}"
    return ingestion


__all__ = ["router"]
