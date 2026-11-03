"""Deprecated listing of the connections ingestion can be submitted for.

Superseded by ``GET /v1/admin/connections``. Each item's ``connection_id`` is
the connection's ``source_key``, which ``POST /v1/ingestions`` accepts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses

from .dependencies import ConnectionCatalogDependency
from .schemas import ConnectionPage

router = APIRouter(
    prefix="/connections",
    tags=["Connections"],
    dependencies=[Depends(require_api_capacity), Depends(require_role("reader"))],
)

ERROR_RESPONSES = documented_error_responses(
    {500: "The connection store could not be read", 503: "The control database is unavailable"}
)


@router.get(
    "",
    response_model=ConnectionPage,
    responses=ERROR_RESPONSES,
    deprecated=True,
)
async def list_connections(
    service: ConnectionCatalogDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ConnectionPage:
    """Connections in the caller's tenants, alphabetically by ``connection_id``."""

    return ConnectionPage.model_validate(
        await service.list_connections(tenant_ids=principal.tenant_scope)
    )
