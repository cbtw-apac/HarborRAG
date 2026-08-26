"""Deterministic external workflow identities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeWorkflowRef:
    """Stable identifiers returned when a source workflow is submitted."""

    run_id: str
    workflow_id: str
    first_execution_run_id: str | None
