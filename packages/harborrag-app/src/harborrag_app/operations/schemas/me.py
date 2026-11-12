"""Response model of ``GET /v1/me``; importable without FastAPI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from harborrag_core.domain.member import Role


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
