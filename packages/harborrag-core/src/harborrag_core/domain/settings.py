"""Workspace-level settings blob, persisted as one JSON document."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class WorkspaceSettings:
    """Schemaless settings document; typed keys can be promoted to fields later."""

    data: dict[str, Any] = field(default_factory=dict)
