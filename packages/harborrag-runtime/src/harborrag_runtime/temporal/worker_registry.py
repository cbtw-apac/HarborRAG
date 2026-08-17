from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG
from harborrag_runtime.temporal_models import TaskQueueConfig

from .document_workflow import DocumentIngestionWorkflow
from .ingestion_activities import IngestionActivities
from .maintenance_activities import MaintenanceActivities
from .reindex_workflow import ReindexWorkflow
from .retry_workflow import DocumentRetryWorkflow, RetryFailuresWorkflow
from .source_batch_workflow import SourceBatchWorkflow
from .source_workflow import SourceIngestionWorkflow


def _stage_activities(
    activities: IngestionActivities,
    role: str,
) -> tuple[Callable[..., Any], ...]:
    return tuple(
        getattr(activities, stage.method_name)
        for stage in DOCUMENT_STAGE_CATALOG
        if stage.task_queue_role == role
    )


def worker_registrations(
    activities: IngestionActivities,
    maintenance: MaintenanceActivities,
    task_queues: TaskQueueConfig = TaskQueueConfig(),
) -> tuple[
    tuple[str, tuple[type[Any], ...], tuple[Callable[..., Any], ...]],
    ...,
]:
    return (
        (
            task_queues.discovery,
            (SourceIngestionWorkflow, RetryFailuresWorkflow),
            (
                activities.discover_source_items,
                activities.cancel_source_ingestion,
                activities.record_source_failure,
                activities.finalize_source_ingestion,
                activities.prepare_retry_failures,
                activities.record_retry_failures_task_failure,
                activities.finalize_retry_failures,
            ),
        ),
        (
            task_queues.transform,
            (
                SourceBatchWorkflow,
                DocumentIngestionWorkflow,
                DocumentRetryWorkflow,
            ),
            _stage_activities(activities, "transform"),
        ),
        (
            task_queues.io,
            (),
            (
                activities.fetch_and_capture_raw,
                activities.record_document_failure,
                activities.retry_document_release,
                activities.record_retry_document_failure,
            )
            + _stage_activities(activities, "io"),
        ),
        (
            task_queues.parser,
            (),
            (activities.parse_and_normalize,),
        ),
        (
            task_queues.model,
            (),
            _stage_activities(activities, "model"),
        ),
        (
            task_queues.index,
            (ReindexWorkflow,),
            (
                *_stage_activities(activities, "index"),
                activities.publish_version,
                maintenance.cleanup_source_projections,
                maintenance.cleanup_reindex_projections,
                maintenance.repair_reindex_relations,
                maintenance.reindex,
            ),
        ),
    )


def validate_worker_registrations(
    registrations: tuple[
        tuple[str, tuple[type[Any], ...], tuple[Callable[..., Any], ...]],
        ...,
    ],
    expected_task_queues: tuple[str, ...] | None = None,
) -> None:
    if expected_task_queues is None:
        expected_task_queues = TaskQueueConfig().as_tuple()
    queue_names = tuple(task_queue for task_queue, _, _ in registrations)
    if set(queue_names) != set(expected_task_queues):
        missing = sorted(set(expected_task_queues) - set(queue_names))
        unexpected = sorted(set(queue_names) - set(expected_task_queues))
        raise RuntimeError(
            f"Temporal worker queue registration mismatch missing={missing} unexpected={unexpected}"
        )

    workflow_names = [
        workflow_type.__name__ for _, workflows, _ in registrations for workflow_type in workflows
    ]
    activity_names = [
        activity_fn.__name__ for _, _, activities in registrations for activity_fn in activities
    ]

    duplicate_workflows = sorted(
        {name for name in workflow_names if workflow_names.count(name) > 1}
    )
    duplicate_activities = sorted(
        {name for name in activity_names if activity_names.count(name) > 1}
    )
    if duplicate_workflows or duplicate_activities:
        raise RuntimeError(
            "Temporal worker registrations contain duplicates "
            f"duplicate_workflows={duplicate_workflows} "
            f"duplicate_activities={duplicate_activities}"
        )
