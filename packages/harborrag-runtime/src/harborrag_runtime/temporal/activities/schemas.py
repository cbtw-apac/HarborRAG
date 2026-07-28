from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ActivityTelemetryContext:
    """Sanitized identifiers and measurements for one activity event."""

    run_id: str
    artifact_id: str | None = None
    artifact_revision_id: str | None = None
    generation_id: str | None = None
    measurements: Mapping[str, int | float] = field(default_factory=dict)
