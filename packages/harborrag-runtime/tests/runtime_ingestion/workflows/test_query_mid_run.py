"""Querying status/progress mid-run must not disturb the workflow (Jira src-query-016).

Start a 20-document ingestion, issue `get_status`/`get_progress` while ~8 of
20 documents are complete and the workflow is still actively running, and
confirm the returned values match the in-flight state exactly -- and that
querying itself has no effect on execution (queries are read-only in
Temporal; nothing here could pause or kill the workflow even by accident).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    SourceDiscoveryResult,
    SourceIngestionResult,
)
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .fixtures import plan_reference as _plan_reference
from .fixtures import source_input as _source

DOCUMENT_COUNT = 20


class _ChildHandle:
    def __init__(self, awaitable) -> None:
        self._task = asyncio.create_task(awaitable)

    def __await__(self):
        return self._task.__await__()

    async def signal(self, name: str) -> None:
        raise AssertionError(f"unexpected signal {name!r} in a query-only scenario")


async def _until(predicate, *, attempts: int = 10_000) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never satisfied")


@pytest.fixture(autouse=True)
def _workflow_wait_condition(monkeypatch):
    async def wait_condition(predicate):
        while not predicate():
            await asyncio.sleep(0)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.wait_condition",
        wait_condition,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.info",
        lambda: type(
            "_WorkflowInfoStub",
            (),
            {"is_continue_as_new_suggested": staticmethod(lambda: False)},
        )(),
    )


@pytest.mark.asyncio
async def test_query_mid_run_reports_in_flight_progress_without_disturbing_the_workflow(
    monkeypatch,
):
    plan = _plan_reference()
    # batch_size=8 (== document_concurrency's default of 8) makes each outer
    # batch land on a clean checkpoint: 8, then 8, then the final 4 -- so
    # "8 of 20 complete" is exactly the boundary after the first batch, before
    # the second one has been dispatched.
    source = replace(_source(), batch_size=8)

    batch_calls: list[int] = []
    hold_second_batch = asyncio.Event()

    async def execute_activity(name, request, **options):
        del options
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=DOCUMENT_COUNT,
            )
        if name == "harborrag.cleanup_source_projections":
            from harborrag_runtime.temporal.maintenance_schemas import (
                ProjectionCleanupResult,
            )

            return ProjectionCleanupResult(claimed=0, completed=0, cancelled=0, failed=0)
        assert name == "harborrag.finalize_source_ingestion"
        return SourceIngestionResult(
            task_id="task-1",
            scan_id="scan-1",
            discovered=DOCUMENT_COUNT,
            published=request.summary.published,
            unchanged=request.summary.unchanged,
            failed=request.summary.failed,
            removal_candidates=(),
            unresolved_relations=0,
        )

    async def child(name, request, **options):
        del options
        assert name == "harborrag.source_batch"
        batch_calls.append(request.batch_number)
        if request.batch_number == 1:
            # The second outer batch (documents 8-15) must not even start
            # dispatching until the test releases it, so the query below is
            # guaranteed to observe exactly one completed batch.
            await hold_second_batch.wait()
        published = request.end_index - request.start_index
        return DocumentDispatchSummary(published=published)

    async def start_child_workflow(name, request, **options):
        del options
        return _ChildHandle(child(name, request))

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        start_child_workflow,
    )

    instance = SourceIngestionWorkflow()
    run_task = asyncio.create_task(instance.run(source))

    # Wait until the first batch (8 documents) has fully merged into the
    # workflow's own summary, and the second batch's dispatch has begun (and
    # is now blocked) -- i.e. the workflow is genuinely still running, not
    # idle between activities.
    await _until(lambda: 1 in batch_calls)
    await _until(lambda: instance.get_progress()["completed_batches"] == 1)

    # --- issue the query ---
    status = instance.get_status()
    progress = instance.get_progress()

    completed = progress["published"] + progress["unchanged"] + progress["failed"]
    remaining = progress["discovered"] - completed

    assert status.status == "RUNNING"
    assert status.paused is False
    assert status.cancel_requested is False
    assert progress["discovered"] == DOCUMENT_COUNT
    assert completed == 8
    assert remaining == 12
    assert progress["failed"] == 0

    # The query must be read-only: the workflow is exactly as it was before
    # the query, still mid-flight on the second batch.
    assert not run_task.done()
    assert batch_calls == [0, 1]
    assert instance.get_status() == status
    assert instance.get_progress() == progress

    # Let the run finish normally and confirm nothing was lost or altered.
    hold_second_batch.set()
    result = await run_task

    assert batch_calls == [0, 1, 2]
    assert result.status == "COMPLETED"
    assert result.discovered == DOCUMENT_COUNT
    assert result.published == DOCUMENT_COUNT
    final_progress = instance.get_progress()
    assert final_progress["published"] == DOCUMENT_COUNT
    assert final_progress["completed_batches"] == 3
