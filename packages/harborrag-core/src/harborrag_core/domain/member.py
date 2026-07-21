"""Workspace membership and the RBAC role ladder.

Role is defined here in core — the single source of truth. Principal
and the members table both import it; never redefine the literal elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["owner", "admin", "editor", "reader"]


@dataclass(slots=True)
class Member:
    """A workspace member: an auth subject bound to a role."""

    id: str
    subject: str
    role: Role = "reader"
