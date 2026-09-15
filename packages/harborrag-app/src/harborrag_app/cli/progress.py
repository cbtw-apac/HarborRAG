"""Progress snapshots and the sources that produce them.

Both executors persist progress in the control-plane task store, but they expose it
through different payloads: ``ingest status`` returns the Temporal status envelope,
``get_task`` returns the public task document. ``ProgressSnapshot`` is the one shape the
inline renderer understands, so a new source only has to build a snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from harborrag_app.cli.rendering_values import mapping, sequence
from harborrag_app.cli.stages import headline_status
from harborrag_app.workflow_control import BaseAppService
from harborrag_app.workflow_control.errors import IngestionNotFoundError

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Public task names (presenters.STATUS_NAMES) -> the run vocabulary stages.py renders.
_TASK_STATUSES = {
    "PENDING": "pending",
    "RUNNING": "running",
    "SUCCESS": "completed",
    "PARTIAL": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
}


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    run_id: str
    status: str
    progress: Mapping[str, Any]
    stage: str | None = None
    message: str | None = None
    failed_artifacts: tuple[str, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "progress": dict(self.progress),
            "message": self.message,
            "failed_artifacts": list(self.failed_artifacts),
        }

    @classmethod
    def from_status_payload(cls, data: Mapping[str, Any]) -> ProgressSnapshot:
        status = mapping(data.get("status"))
        progress = mapping(data.get("progress")) or mapping(status.get("progress"))
        workflow = str(status.get("status", "unknown")).lower()
        execution = str(data.get("execution_status", "")).lower()
        return cls(
            run_id=str(status.get("run_id", "—")),
            status=headline_status(workflow, execution),
            progress=dict(progress),
            failed_artifacts=tuple(str(item) for item in sequence(data.get("failed_artifacts"))),
        )

    @classmethod
    def from_task_payload(cls, data: Mapping[str, Any]) -> ProgressSnapshot:
        name = str(data.get("status", "")).upper()
        stage = data.get("stage")
        message = data.get("message")
        return cls(
            run_id=str(data.get("task_id", "—")),
            status=_TASK_STATUSES.get(name, "unknown"),
            progress=dict(mapping(data.get("progress"))),
            stage=str(stage) if stage else None,
            message=str(message) if message else None,
        )


class ProgressUnavailable(RuntimeError):
    """The source could not produce a snapshot; the caller decides whether to stop."""


class ProgressSource(Protocol):
    async def snapshot(self) -> ProgressSnapshot | None: ...


class StatusSource:
    """Snapshots from the Temporal-backed ``ingestion_status`` call."""

    def __init__(self, service: BaseAppService, run_id: str) -> None:
        self._service = service
        self._run_id = run_id

    async def snapshot(self) -> ProgressSnapshot | None:
        response = await self._service.ingestion_status(self._run_id)
        if not response.ok:
            raise ProgressUnavailable(response.error or "Unable to load ingestion status.")
        return ProgressSnapshot.from_status_payload(response.data)


class TaskReader(Protocol):
    async def get_task(self, task_id: str) -> dict[str, object]: ...


class TaskSource:
    """Snapshots from the control-plane task store while a direct run is in flight."""

    def __init__(self, reader: TaskReader, task_id: str) -> None:
        self._reader = reader
        self._task_id = task_id

    async def snapshot(self) -> ProgressSnapshot | None:
        try:
            data = await self._reader.get_task(self._task_id)
        except IngestionNotFoundError:
            # Registration happens inside the run; until then there is nothing to show.
            return None
        return ProgressSnapshot.from_task_payload(data)


__all__ = [
    "TERMINAL_STATUSES",
    "ProgressSnapshot",
    "ProgressSource",
    "ProgressUnavailable",
    "StatusSource",
    "TaskReader",
    "TaskSource",
]
