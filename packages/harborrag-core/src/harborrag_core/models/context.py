from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ModelOperationContext:
    """Carry mutable execution identity across model middleware and telemetry."""

    request_id: str
    logical_model: str
    trace_id: str | None = None
    span_id: str | None = None
    deployment: str | None = None
    provider: str | None = None
    provider_model: str | None = None
    retry_count: int = 0
    fallback_count: int = 0
    cache_hit: bool = False
    state: dict[str, Any] = field(default_factory=dict)
