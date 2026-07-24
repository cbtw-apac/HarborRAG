"""Sanitized activity telemetry context for the process-owned observer."""

from __future__ import annotations

from temporalio import activity

from harborrag_runtime.temporal.dependencies import RuntimeDependencies

from .schemas import ActivityTelemetryContext


def record_activity(
    dependencies: RuntimeDependencies,
    event: str,
    context: ActivityTelemetryContext,
) -> None:
    info = activity.info()
    attributes: dict[str, str | int | float] = {
        "activity": info.activity_type,
        "attempt": info.attempt,
        "ingestion_run_id": context.run_id,
        "task_queue": info.task_queue,
    }
    optional = {
        "workflow_id": info.workflow_id,
        "workflow_run_id": info.workflow_run_id,
        "artifact_id": context.artifact_id,
        "artifact_revision_id": context.artifact_revision_id,
        "generation_id": context.generation_id,
    }
    attributes.update({key: value for key, value in optional.items() if value is not None})
    attributes.update(context.measurements)
    dependencies.observer.record(event, attributes)
