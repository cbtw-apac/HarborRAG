"""Retry-activity and plan-resolver coverage split from ingestion activity tests."""

from __future__ import annotations

from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal import retry_activities as retry_module
from harborrag_runtime.temporal.dispatch import DocumentDispatchSummary
from harborrag_runtime.temporal.plan_resolver import PlanDocumentResolver
from harborrag_runtime.temporal.schemas import (
    DocumentIngestionInput,
    RetryDocumentFailureInput,
    RetryDocumentInput,
    RetryFailuresInput,
    RetryFinalizationInput,
    RetryTaskFailureInput,
)

from .test_ingestion_activities import (
    FixedDocumentResolver,
    RecordingPlans,
    _artifact,
    _build_activities,
    _planned_document,
)

pytestmark = pytest.mark.whitebox


@pytest.mark.asyncio
async def test_retry_activities_cover_selection_release_failure_and_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities, _, _, sources, _, connector_calls = _build_activities()
    planned = _planned_document()
    plans = RecordingPlans((planned,))
    cast(Any, activities._runtime).source_plans = plans
    activities._documents = cast(Any, FixedDocumentResolver(planned))
    monkeypatch.setattr(retry_module, "to_workflow_artifact", lambda _reference: _artifact())

    selection = RetryFailuresInput("retry-1", "original-1", "tenant-1", ("doc-1",))
    result = await activities.prepare_retry_failures(selection)
    assert result.plan_reference == _artifact() and result.document_count == 1

    retry_document = RetryDocumentInput("retry-1", "original-1", "tenant-1", _artifact(), 0)
    assert (
        await activities.retry_document_release(retry_document)
        is DocumentIngestionOutcome.UNCHANGED
    )
    await activities.record_retry_document_failure(
        RetryDocumentFailureInput(retry_document, "ProjectionError")
    )
    await activities.record_retry_failures_task_failure(
        RetryTaskFailureInput("retry-1", "retry_failed")
    )
    await activities.finalize_retry_failures(
        RetryFinalizationInput("retry-1", 1, DocumentDispatchSummary(unchanged=1))
    )

    assert connector_calls == [("jira-main", "config-v1")]
    assert plans.calls == ["find", "get", "put"]
    assert [name for name, _, _ in sources.calls] == [
        "begin_retry",
        "retry_one",
        "record_retry_failure",
        "fail_retry",
        "finish_retry",
    ]

    plans.find_result = None
    with pytest.raises(ValueError, match="source plan is unavailable"):
        await activities.prepare_retry_failures(selection)
    plans.find_result = object()
    outside_plan = RetryFailuresInput("retry-2", "original-1", "tenant-1", ("other",))
    with pytest.raises(ValueError, match="outside the source plan"):
        await activities.prepare_retry_failures(outside_plan)


@pytest.mark.asyncio
async def test_plan_resolver_rejects_an_out_of_range_document_index() -> None:
    plans = RecordingPlans(())
    resolver = PlanDocumentResolver(cast(Any, plans))
    request = DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), 3)

    with pytest.raises(ApplicationError, match="document index is invalid") as raised:
        await resolver.get(request)
    assert raised.value.non_retryable is True


@pytest.mark.asyncio
async def test_plan_resolver_rejects_a_negative_document_index() -> None:
    """A negative index must not silently select from the end of the plan."""
    plans = RecordingPlans((_planned_document(),))
    resolver = PlanDocumentResolver(cast(Any, plans))
    request = DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), -1)

    with pytest.raises(ApplicationError, match="document index is invalid") as raised:
        await resolver.get(request)
    assert raised.value.non_retryable is True
