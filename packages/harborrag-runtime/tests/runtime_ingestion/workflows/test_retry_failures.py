"""Durable retry-failures workflow behavior."""

from __future__ import annotations

import pytest
from temporalio.exceptions import ChildWorkflowError

from harborrag_runtime.temporal.retry_workflow import RetryFailuresWorkflow
from harborrag_runtime.temporal.schemas import RetryFailuresInput, RetryPreparationResult

from .fixtures import plan_reference as _plan_reference


def _request() -> RetryFailuresInput:
    return RetryFailuresInput(
        retry_task_id="retry-1",
        original_task_id="task-1",
        tenant_id="tenant-1",
        document_ids=("document-1", "document-2"),
        document_concurrency=2,
    )


@pytest.mark.asyncio
async def test_retry_failures_workflow_records_failure_and_reraises_when_child_fails(
    monkeypatch,
) -> None:
    """A hard child document-retry failure used to propagate straight out of
    `run()` with no failure record, leaving the control-plane retry-task row
    stuck non-terminal even though Temporal itself considered the run
    failed. It must now be recorded before the failure is re-raised."""
    plan = _plan_reference()
    recorded = []

    async def execute_activity(name, request, **options):
        del options
        if name == "harborrag.prepare_retry_failures":
            return RetryPreparationResult(plan_reference=plan, document_count=2)
        assert name == "harborrag.record_retry_failures_task_failure"
        recorded.append(request)
        return None

    async def child(name, request, **options):
        del request, options
        assert name == "harborrag.document_retry"
        raise ChildWorkflowError(
            "child failed",
            namespace="default",
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="harborrag.document_retry",
            initiated_event_id=1,
            started_event_id=2,
            retry_state=None,
        )

    monkeypatch.setattr(
        "harborrag_runtime.temporal.retry_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.retry_workflow.workflow.execute_child_workflow",
        child,
    )

    with pytest.raises(ChildWorkflowError):
        await RetryFailuresWorkflow().run(_request())

    assert len(recorded) == 1
    assert recorded[0].retry_task_id == "retry-1"
    assert recorded[0].error_code == "ChildWorkflowError"


@pytest.mark.asyncio
async def test_retry_failures_workflow_finalizes_normally_when_children_succeed(
    monkeypatch,
) -> None:
    plan = _plan_reference()

    async def execute_activity(name, request, **options):
        del options
        if name == "harborrag.prepare_retry_failures":
            return RetryPreparationResult(plan_reference=plan, document_count=2)
        assert name == "harborrag.finalize_retry_failures"
        return None

    async def child(name, request, **options):
        del options
        assert name == "harborrag.document_retry"
        return "published" if request.document_index == 0 else "unchanged"

    monkeypatch.setattr(
        "harborrag_runtime.temporal.retry_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.retry_workflow.workflow.execute_child_workflow",
        child,
    )

    result = await RetryFailuresWorkflow().run(_request())

    assert result.status == "COMPLETED"
    assert result.published == 1
    assert result.unchanged == 1
