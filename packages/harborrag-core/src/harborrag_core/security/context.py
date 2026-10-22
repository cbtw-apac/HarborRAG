from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import TenantId

# Reserved tenant of platform-scoped contexts. It names no real tenant, so a
# repository that enforces ``context.tenant_id`` matches no rows for it, and
# ``AccessContext`` refuses it as the tenant of a tenant-scoped context.
PLATFORM_TENANT_ID = TenantId("__platform__")


class AccessContext(StrictModel):
    """Required principal and tenant boundary for every HarborRAG operation.

    ``scope`` is ``tenant`` for everything bound to one tenant. ``platform`` is
    the explicit administrative scope for operations that are global by nature
    (looking a key up by its id before its tenant is known, tenant discovery).
    """

    principal_id: str = Field(min_length=1, max_length=255)
    tenant_id: TenantId
    corpus_mode: Literal["source_acl", "tenant_shared"] = "source_acl"
    scope: Literal["tenant", "platform"] = "tenant"

    @model_validator(mode="after")
    def _scope_matches_tenant(self) -> AccessContext:
        if (self.scope == "platform") != (self.tenant_id == PLATFORM_TENANT_ID):
            raise ValueError("platform scope and the reserved platform tenant go together")
        return self

    @classmethod
    def platform(cls, principal_id: str) -> AccessContext:
        """Build the explicit administrative identity for operations with no tenant."""

        return cls(principal_id=principal_id, tenant_id=PLATFORM_TENANT_ID, scope="platform")

    @classmethod
    def system(cls, tenant_id: str | TenantId) -> AccessContext:
        """Build the explicit identity used by trusted background execution."""

        return cls(principal_id="harborrag-runtime", tenant_id=TenantId(tenant_id))
