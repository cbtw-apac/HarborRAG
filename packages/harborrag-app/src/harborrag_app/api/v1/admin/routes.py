"""Authenticated, tenant-scoped administration of rebuildable projections."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Path, Query

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_core.contracts.errors import HarborValidationError

from .dependencies import ProjectionAdminServiceDependency
from .schemas import ProjectionDeletionResponse, ProjectionInventoryResponse

router = APIRouter(prefix="/admin/projections", tags=["Administration"])
logger = logging.getLogger("harborrag.app.api.admin.projections")

TenantPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Tenant projection namespace.",
    ),
]

ERROR_RESPONSES = documented_error_responses(
    {
        403: "Administrator role required",
        422: "Invalid tenant, store, or confirmation",
        503: "Projection storage unavailable",
    }
)


@router.get(
    "/{tenant}",
    response_model=ProjectionInventoryResponse,
    responses=ERROR_RESPONSES,
)
async def inspect_projections(
    tenant: TenantPath,
    service: ProjectionAdminServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> ProjectionInventoryResponse:
    authorize_tenant(principal, tenant)
    result = ProjectionInventoryResponse.model_validate(await service.projection_inventory(tenant))
    logger.info(
        "Projection inventory read principal=%s tenant=%s graph_nodes=%d graph_relations=%d",
        principal.subject,
        tenant,
        result.graph_nodes,
        result.graph_relations,
    )
    return result


@router.delete(
    "/{tenant}",
    response_model=ProjectionDeletionResponse,
    responses=ERROR_RESPONSES,
)
async def delete_projections(
    tenant: TenantPath,
    service: ProjectionAdminServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    confirmation: Annotated[
        str,
        Header(
            alias="X-Confirm-Tenant",
            min_length=1,
            max_length=128,
            description="Must exactly match the tenant path value.",
        ),
    ],
    stores: Annotated[
        list[Literal["vector", "graph"]] | None,
        Query(description="Projection stores to delete; defaults to both."),
    ] = None,
) -> ProjectionDeletionResponse:
    authorize_tenant(principal, tenant)
    if confirmation != tenant:
        raise HarborValidationError("X-Confirm-Tenant must exactly match the tenant path value")
    selected_stores = frozenset(stores or ("vector", "graph"))
    logger.warning(
        "Projection deletion requested principal=%s tenant=%s stores=%s",
        principal.subject,
        tenant,
        ",".join(sorted(selected_stores)),
    )
    result = ProjectionDeletionResponse.model_validate(
        await service.delete_projections(
            tenant,
            confirmation=confirmation,
            stores=selected_stores,
        )
    )
    logger.warning(
        "Projection deletion completed principal=%s tenant=%s stores=%s",
        principal.subject,
        tenant,
        ",".join(result.deleted_stores),
    )
    return result
