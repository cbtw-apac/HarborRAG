from __future__ import annotations

from typing import Literal

from pydantic import Field

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security.decisions import AccessScope


class AccessContext(StrictModel):
    """Required principal and tenant boundary for every HarborRAG operation.

    ``principal_id`` and ``tenant_id`` are the identity the request runs as.
    The remaining fields describe what that identity may read, through three
    gates: the tenant, the datasources its AUTA RBAC role entitles it to
    (``entitled_source_scopes``) and the source-ACL classes it belongs to
    (``class_ids``). All of them default to "no extra evidence", so a context
    built without them behaves as before; the access service fills them in.

    ``entitled_source_scopes`` is ``None`` only for a tenant with no RBAC
    configured, which skips the entitlement gate. An empty set entitles nothing.
    ``purpose`` separates a request served to a person (``serve``) from trusted
    background work (``process``); a serve check refuses a ``process`` context.
    """

    principal_id: str = Field(min_length=1, max_length=255)
    tenant_id: TenantId
    corpus_mode: Literal["source_acl", "tenant_shared"] = "source_acl"
    actor_id: str | None = Field(default=None, min_length=1, max_length=255)
    class_ids: frozenset[str] = frozenset()
    entitled_source_scopes: frozenset[str] | None = None
    scope: AccessScope = Field(default_factory=AccessScope.full)
    purpose: Literal["serve", "process"] = "serve"

    @classmethod
    def system(cls, tenant_id: str | TenantId) -> AccessContext:
        """Build the explicit identity used by trusted background execution."""

        return cls(
            principal_id="harborrag-runtime", tenant_id=TenantId(tenant_id), purpose="process"
        )
