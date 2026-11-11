from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppResponse:
    """Transport-neutral response returned by workflow-control operations."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
