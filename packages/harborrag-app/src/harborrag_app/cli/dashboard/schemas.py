from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from harborrag_app.cli.stages import headline_status


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Normalized dashboard view of the public ingestion status response.

    ``status`` is reconciled against Temporal's execution status so a run whose
    workflow crashed settles instead of reporting itself as running forever.
    ``workflow_status`` keeps the workflow's own unreconciled view for display.
    """

    run_id: str
    status: str
    workflow_status: str
    execution_status: str
    current_partition: int | None
    paused: bool
    cancel_requested: bool
    progress: Mapping[str, Any]
    failed_artifacts: tuple[str, ...]
    quarantined_artifacts: tuple[str, ...]
    pending_resolutions: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        fallback_run_id: str,
    ) -> DashboardSnapshot:
        status = as_mapping(payload.get("status"))
        progress = as_mapping(payload.get("progress")) or as_mapping(status.get("progress"))
        workflow_status = as_string(status.get("status"), fallback="unknown").lower()
        execution_status = as_string(payload.get("execution_status"), fallback="").lower()
        return cls(
            run_id=as_string(status.get("run_id"), fallback=fallback_run_id),
            status=headline_status(workflow_status, execution_status),
            workflow_status=workflow_status,
            execution_status=execution_status,
            current_partition=as_optional_integer(status.get("current_partition")),
            paused=bool(status.get("paused", False)),
            cancel_requested=bool(status.get("cancel_requested", False)),
            progress=progress,
            failed_artifacts=as_strings(payload.get("failed_artifacts")),
            quarantined_artifacts=as_strings(payload.get("quarantined_artifacts")),
            pending_resolutions=tuple(
                as_mapping(item) for item in as_sequence(payload.get("pending_resolutions"))
            ),
        )


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def as_strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in as_sequence(value))


def as_string(value: Any, *, fallback: str) -> str:
    return str(value) if value is not None and str(value) else fallback


def as_integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def as_optional_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
