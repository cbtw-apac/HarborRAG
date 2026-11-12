"""Authenticated principal and the RBAC role ladder (ST4, plan §8.1).

Role itself is owned by harborrag_core.domain.member — the single source of
truth shared with the members table; this module only adds ordering.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.domain.member import Role, ensure_role


@dataclass(slots=True, frozen=True)
class Principal:
    """Who is calling: auth subject, effective role, and how they proved it.

    Args:
        subject: Authenticated subject of the credential.
        role: Effective role after the credential's role is applied.
        tenant_ids: Tenants the credential may act in.
        token_kind: Credential kind, ``jwt`` unless it is an API key.
        user_id: Trusted end-user claim supplied by the verifier; callers without
            one, such as unauthenticated local development, keep the default.
        name: Display name of the caller, empty when unknown.
    """

    subject: str
    role: Role
    tenant_ids: frozenset[str]
    token_kind: str = "jwt"
    user_id: str = DEFAULT_USER
    name: str = ""

    def __post_init__(self) -> None:
        if not self.user_id or not self.user_id.strip():
            raise ValueError("principal user_id must not be blank")

    @property
    def kind(self) -> str:
        """``api_key`` for an API key, ``user`` for every other credential."""
        return "api_key" if self.token_kind == "api_key" else "user"

    def can_access_tenant(self, tenant_id: str) -> bool:
        """Return whether the principal may act on the tenant."""
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


def authorize_role(principal: Principal, minimum: Role) -> None:
    """Reject a principal below the minimum role.

    Raises:
        HarborAuthError: The principal's role is below ``minimum``.
    """
    ensure_role(principal.role, minimum)
