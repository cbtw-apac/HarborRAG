"""``GET /v1/me``: what the server read from the caller's credential."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.auth.tenancy import OptionalTenantDependency
from harborrag_app.operations.contract import OPERATIONS, route_kwargs

from .me_schemas import MeOut, MePrincipal

router = APIRouter(tags=["Identity"])


@router.get("/me", summary="Who am I", **route_kwargs("me"))
async def me(
    principal: Annotated[Principal, Depends(require_role(OPERATIONS["me"].role, tenant=False))],
    tenant: OptionalTenantDependency,
) -> MeOut:
    """Describe the credential, so a client can pick its screens and its ``X-Tenant-Id``."""
    return MeOut(
        principal=MePrincipal(
            principal_type=principal.kind,  # type: ignore[arg-type]
            principal_id=principal.subject,
            name=principal.name or principal.subject,
        ),
        tenant_id=tenant,
        tenants=sorted(principal.tenant_ids),
        role=principal.role,
    )
