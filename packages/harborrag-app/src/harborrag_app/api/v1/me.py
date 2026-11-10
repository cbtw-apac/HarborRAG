"""``GET /v1/me``: what the server read from the caller's credential."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.auth.tenancy import OptionalTenantDependency
from harborrag_app.api.dependencies import OperationsDependency
from harborrag_app.operations.contract import OPERATIONS, route_kwargs

from .me_schemas import MeOut

router = APIRouter(tags=["Identity"])


@router.get("/me", summary="Who am I", **route_kwargs("me"))
async def me(
    principal: Annotated[Principal, Depends(require_role(OPERATIONS["me"].role, tenant=False))],
    tenant: OptionalTenantDependency,
    operations: OperationsDependency,
) -> MeOut:
    """Describe the credential, so a client can pick its screens and its ``X-Tenant-Id``."""
    return await operations.me(principal, tenant_id=tenant)
