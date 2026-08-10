"""Authenticated principal and the RBAC role ladder (ST4, plan §8.1).

Role itself is owned by harborrag_core.domain.member — the single source of
truth shared with the members table; this module only adds ordering.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.domain.member import Role

ROLE_ORDER: dict[Role, int] = {"reader": 0, "editor": 1, "admin": 2, "owner": 3}


@dataclass(slots=True, frozen=True)
class Principal:
    """Who is calling: auth subject, effective role, and how they proved it."""

    subject: str
    role: Role
    tenant_ids: frozenset[str]
    token_kind: str = "jwt"

    def can_access_tenant(self, tenant_id: str) -> bool:
        return "*" in self.tenant_ids or tenant_id in self.tenant_ids

    @property
    def tenant_scope(self) -> frozenset[str] | None:
        """This principal's repository-layer filter: None means unrestricted.

        Every control-plane repository read/delete takes a ``tenant_ids``
        filter where None means "no restriction" -- reserved for trusted
        system callers. Wildcard principals (dev/no-auth, "*") are the one
        application-facing exception: they are meant to see every tenant.
        """
        return None if "*" in self.tenant_ids else self.tenant_ids
