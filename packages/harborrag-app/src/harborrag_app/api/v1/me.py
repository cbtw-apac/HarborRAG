"""``GET /v1/me``: what the server read from the caller's credential."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.auth.tenancy import OptionalTenantDependency
from harborrag_core.domain.member import Role

router = APIRouter(tags=["Identity"])


class MePrincipal(BaseModel):
    """The person or API key behind the credential."""

    principal_type: Literal["user", "api_key"]
    principal_id: str = Field(description="The token's sub claim, or the key ID")
    name: str


class MeOut(BaseModel):
    """The credential's principal, role and tenants."""

    principal: MePrincipal
    tenant_id: str | None = Field(
        description="The tenant this call acted in; null when several are held and none was named"
    )
    tenants: list[str] = Field(
        description="Every tenant the credential can act in; more than one means send X-Tenant-Id"
    )
    role: Role


@router.get("/me", summary="Who am I", response_model=MeOut)
async def me(
    principal: Annotated[Principal, Depends(require_role("reader", tenant=False))],
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


__all__ = ["router"]
