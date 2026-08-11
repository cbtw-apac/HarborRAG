from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .document_workflow import DocumentIngestionWorkflow
from .ingestion_activities import IngestionActivities
from .maintenance_activities import MaintenanceActivities
from .policies import (
    DISCOVERY_QUEUE,
    INDEX_QUEUE,
    IO_QUEUE,
    MODEL_QUEUE,
    PARSER_QUEUE,
    TRANSFORM_QUEUE,
)
from .reindex_workflow import ReindexWorkflow
from .retry_workflow import DocumentRetryWorkflow, RetryFailuresWorkflow
from .source_workflow import SourceBatchWorkflow, SourceIngestionWorkflow

ALL_TASK_QUEUES = (
    DISCOVERY_QUEUE,
    TRANSFORM_QUEUE,
    IO_QUEUE,
    PARSER_QUEUE,
    MODEL_QUEUE,
    INDEX_QUEUE,
)
EXPECTED_WORKFLOWS = {
    "SourceIngestionWorkflow",
    "RetryFailuresWorkflow",
    "SourceBatchWorkflow",
    "DocumentIngestionWorkflow",
    "DocumentRetryWorkflow",
    "ReindexWorkflow",
}
EXPECTED_ACTIVITIES = {
    "discover_source_items",
    "cancel_source_ingestion",
    "record_source_failure",
    "finalize_source_ingestion",
    "prepare_retry_failures",
    "record_retry_failures_task_failure",
    "finalize_retry_failures",
    "sync_content_units",
    "chunk_and_validate",
    "build_relations",
    "build_projections",
    "fetch_and_capture_raw",
    "persist_canonical",
    "record_document_failure",
    "retry_document_release",
    "record_retry_document_failure",
    "parse_and_normalize",
    "encode_chunks",
    "write_vector_projection",
    "write_graph_projection",
    "verify_projections",
    "publish_version",
    "cleanup_source_projections",
    "cleanup_reindex_projections",
    "repair_reindex_relations",
    "reindex",
}


def worker_registrations(
    activities: IngestionActivities,
    maintenance: MaintenanceActivities,
) -> tuple[
    tuple[str, tuple[type[Any], ...], tuple[Callable[..., Any], ...]],
    ...,
]:
    return (
        (
            DISCOVERY_QUEUE,
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
            TRANSFORM_QUEUE,
            (
                SourceBatchWorkflow,
                DocumentIngestionWorkflow,
                DocumentRetryWorkflow,
            ),
            (
                activities.sync_content_units,
                activities.chunk_and_validate,
                activities.build_relations,
                activities.build_projections,
            ),
        ),
        (
            IO_QUEUE,
            (),
            (
                activities.fetch_and_capture_raw,
                activities.persist_canonical,
                activities.record_document_failure,
                activities.retry_document_release,
                activities.record_retry_document_failure,
            ),
        ),
        (
            PARSER_QUEUE,
            (),
            (activities.parse_and_normalize,),
        ),
        (
            MODEL_QUEUE,
            (),
            (activities.encode_chunks,),
        ),
        (
            INDEX_QUEUE,
            (ReindexWorkflow,),
            (
                activities.write_vector_projection,
                activities.write_graph_projection,
                activities.verify_projections,
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
) -> None:
    queue_names = tuple(task_queue for task_queue, _, _ in registrations)
    if set(queue_names) != set(ALL_TASK_QUEUES):
        missing = sorted(set(ALL_TASK_QUEUES) - set(queue_names))
        unexpected = sorted(set(queue_names) - set(ALL_TASK_QUEUES))
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
    missing_workflows = sorted(EXPECTED_WORKFLOWS - set(workflow_names))
    missing_activities = sorted(EXPECTED_ACTIVITIES - set(activity_names))
    unexpected_workflows = sorted(set(workflow_names) - EXPECTED_WORKFLOWS)
    unexpected_activities = sorted(set(activity_names) - EXPECTED_ACTIVITIES)
    if any(
        (
            duplicate_workflows,
            duplicate_activities,
            missing_workflows,
            missing_activities,
            unexpected_workflows,
            unexpected_activities,
        )
    ):
        raise RuntimeError(
            "Temporal worker registrations are incomplete or inconsistent "
            f"duplicate_workflows={duplicate_workflows} "
            f"duplicate_activities={duplicate_activities} "
            f"missing_workflows={missing_workflows} "
            f"missing_activities={missing_activities} "
            f"unexpected_workflows={unexpected_workflows} "
            f"unexpected_activities={unexpected_activities}"
        )
