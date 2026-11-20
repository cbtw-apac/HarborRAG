"""Workspace membership and the RBAC role ladder.

Role is defined here in core — the single source of truth. Principal
and the members table both import it; never redefine the literal elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from harborrag_core.contracts.errors import HarborAuthError

from .validation import require_id, require_tenant_id

Role = Literal["owner", "admin", "editor", "reader"]

ROLE_ORDER: dict[Role, int] = {"reader": 0, "editor": 1, "admin": 2, "owner": 3}


def ensure_role(role: Role, minimum: Role) -> None:
    """Refuse a role below ``minimum``.

    Args:
        role: The caller's effective role.
        minimum: The lowest role that may proceed.

    Raises:
        HarborAuthError: ``role`` is below ``minimum`` (forbidden, not unauthenticated).
    """
    if ROLE_ORDER[role] < ROLE_ORDER[minimum]:
        raise HarborAuthError(f"requires {minimum} role", forbidden=True)


@dataclass(slots=True)
class Member:
    """A workspace member: an auth subject bound to a role."""

    id: str
    subject: str
    tenant_id: str
    role: Role = "reader"

    def __post_init__(self) -> None:
        require_id(self.id, label="Member")
        require_tenant_id(self.tenant_id)
