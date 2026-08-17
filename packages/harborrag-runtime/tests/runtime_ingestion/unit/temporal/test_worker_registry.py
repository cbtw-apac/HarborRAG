"""Temporal worker registration inventory and sandbox compatibility."""

from __future__ import annotations

import pytest
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from harborrag_runtime.temporal import worker_registry as registry
from harborrag_runtime.temporal.document_workflow import DocumentIngestionWorkflow
from harborrag_runtime.temporal.reindex_workflow import ReindexWorkflow
from harborrag_runtime.temporal.retry_workflow import (
    DocumentRetryWorkflow,
    RetryFailuresWorkflow,
)
from harborrag_runtime.temporal.source_batch_workflow import SourceBatchWorkflow
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

WORKFLOWS = (
    SourceIngestionWorkflow,
    SourceBatchWorkflow,
    DocumentIngestionWorkflow,
    DocumentRetryWorkflow,
    RetryFailuresWorkflow,
    ReindexWorkflow,
)


@pytest.mark.parametrize("workflow_type", WORKFLOWS)
@pytest.mark.asyncio
async def test_registered_workflows_validate_in_default_temporal_sandbox(workflow_type) -> None:
    definition = workflow._Definition.must_from_class(workflow_type)

    SandboxedWorkflowRunner().prepare_workflow(definition)


def test_worker_registration_validation_fails_loudly_for_duplicates() -> None:
    registrations = (
        ("harborrag-discovery", (SourceIngestionWorkflow,), ()),
        ("harborrag-transform", (SourceIngestionWorkflow,), ()),
        ("harborrag-io", (), ()),
        ("harborrag-parser", (), ()),
        ("harborrag-model", (), ()),
        ("harborrag-index", (), ()),
    )

    with pytest.raises(RuntimeError, match="duplicate_workflows"):
        registry.validate_worker_registrations(registrations)


def test_worker_registration_inventory_is_complete() -> None:
    activities = object.__new__(registry.IngestionActivities)
    maintenance = object.__new__(registry.MaintenanceActivities)
    registrations = registry.worker_registrations(activities, maintenance)

    workflow_names = {
        workflow_type.__name__ for _, workflows, _ in registrations for workflow_type in workflows
    }
    activity_names = {
        activity_fn.__name__
        for _, _, activity_functions in registrations
        for activity_fn in activity_functions
    }

    assert workflow_names == {workflow_type.__name__ for workflow_type in WORKFLOWS}
    assert activity_names == {
        "build_projections",
        "build_relations",
        "cancel_source_ingestion",
        "chunk_and_validate",
        "cleanup_reindex_projections",
        "cleanup_source_projections",
        "discover_source_items",
        "encode_chunks",
        "fetch_and_capture_raw",
        "finalize_retry_failures",
        "finalize_source_ingestion",
        "parse_and_normalize",
        "persist_canonical",
        "prepare_retry_failures",
        "publish_version",
        "record_document_failure",
        "record_retry_document_failure",
        "record_retry_failures_task_failure",
        "record_source_failure",
        "reindex",
        "repair_reindex_relations",
        "retry_document_release",
        "sync_content_units",
        "verify_projections",
        "write_graph_projection",
        "write_vector_projection",
    }
