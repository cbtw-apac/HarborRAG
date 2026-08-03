from __future__ import annotations

from pydantic import Field

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import TenantId


class AccessContext(StrictModel):
    """Required principal and tenant boundary for every HarborRAG operation."""

    principal_id: str = Field(min_length=1, max_length=255)
    tenant_id: TenantId

    @classmethod
    def system(cls, tenant_id: str | TenantId) -> AccessContext:
        """Build the explicit identity used by trusted background execution."""

        return cls(principal_id="harborrag-runtime", tenant_id=TenantId(tenant_id))
