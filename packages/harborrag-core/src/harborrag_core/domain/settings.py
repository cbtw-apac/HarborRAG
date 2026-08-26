"""Workspace-level settings blob, persisted as one JSON document."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .validation import require_tenant_id, validate_secret_free_config


@dataclass(slots=True)
class WorkspaceSettings:
    """Schemaless settings document; typed keys can be promoted to fields later."""

    tenant_id: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_tenant_id(self.tenant_id)
        validate_secret_free_config(self.data)
