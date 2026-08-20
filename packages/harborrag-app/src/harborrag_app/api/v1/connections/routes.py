"""Read-only listing of the connections ingestion can be submitted for.

``POST /v1/ingestions`` takes a ``connection_id`` naming a key in the worker's
``config/connectors.yaml``, which no HTTP client can read. This route publishes
those identities so a caller can offer or validate the same choices the worker
will accept, without exposing any connector settings or credential references.

The catalog is process-wide configuration rather than tenant data, so the route
enforces the reader role and does not scope by tenant.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.dependencies import require_role
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
    {500: "Connector configuration is unreadable or invalid"}
)


@router.get(
    "",
    response_model=ConnectionPage,
    responses=ERROR_RESPONSES,
)
async def list_connections(service: ConnectionCatalogDependency) -> ConnectionPage:
    """Enabled connections, alphabetically by ``connection_id``."""

    return ConnectionPage.model_validate(await service.list_connections())
