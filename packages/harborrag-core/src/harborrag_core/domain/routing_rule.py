"""RoutingRule: which provider should serve a family of requests.

Workspace-wide, not tenant-scoped (see ``routing_rules`` DDL) -- routing is a
single control-plane-wide policy, not a per-tenant setting, mirroring
``WorkspaceSettings``. Multiple rules may share a ``family``; ``priority``
(lower tries first) lets a caller express a fallback chain within that
family instead of a single hard-coded provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from .provider import ProviderFamily
from .validation import require_id


@dataclass(slots=True)
class RoutingRule:
    """One entry in the routing table: try ``provider_id`` for ``family`` requests."""

    id: str
    family: ProviderFamily
    provider_id: str
    priority: int = 0

    def __post_init__(self) -> None:
        require_id(self.id, label="RoutingRule")
        require_id(self.provider_id, label="RoutingRule provider_id")
