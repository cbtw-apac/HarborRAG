"""Workspace membership and the RBAC role ladder.

Role is defined here in core — the single source of truth. Principal
and the members table both import it; never redefine the literal elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .validation import require_id, require_tenant_id

Role = Literal["owner", "admin", "editor", "reader"]


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
