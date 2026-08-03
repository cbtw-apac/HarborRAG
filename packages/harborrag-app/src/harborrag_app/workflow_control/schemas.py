from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppResponse:
    """Transport-neutral response returned by workflow-control operations."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True, frozen=True)
class JobRunOptions:
    """The Temporal run-identity/tuning knobs create_job passes straight
    through to start_ingestion, bundled so create_job's own signature stays
    within the repo's argument-count gate."""

    run_id: str | None = None
    manifest_id: str | None = None
    generation_id: str | None = None
    max_artifacts: int | None = None
    wait: bool = False
