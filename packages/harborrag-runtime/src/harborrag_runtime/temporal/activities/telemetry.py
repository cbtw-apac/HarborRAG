"""Sanitized activity telemetry context for the process-owned observer."""

from __future__ import annotations

from collections.abc import Mapping

from temporalio import activity

from harborrag_runtime.temporal.dependencies import RuntimeDependencies


def record_activity(
    dependencies: RuntimeDependencies,
    event: str,
    *,
    run_id: str,
    artifact_id: str | None = None,
    artifact_revision_id: str | None = None,
    generation_id: str | None = None,
    measurements: Mapping[str, int | float] | None = None,
) -> None:
    info = activity.info()
    attributes: dict[str, str | int | float] = {
        "activity": info.activity_type,
        "attempt": info.attempt,
        "ingestion_run_id": run_id,
        "task_queue": info.task_queue,
    }
    optional = {
        "workflow_id": info.workflow_id,
        "workflow_run_id": info.workflow_run_id,
        "artifact_id": artifact_id,
        "artifact_revision_id": artifact_revision_id,
        "generation_id": generation_id,
    }
    attributes.update({key: value for key, value in optional.items() if value is not None})
    if measurements is not None:
        attributes.update(measurements)
    dependencies.observer.record(event, attributes)
