"""End-to-end pause/resume for a source ingestion run (Jira src-pause-006).

Start a long-running ingestion, pause it while a batch of documents is
actively in flight, verify the run reports PAUSED and stops starting new
document dispatch, resume it, and confirm it completes with every document
accounted for exactly once.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal.maintenance_schemas import ProjectionCleanupResult
from harborrag_runtime.temporal.schemas import (
    SourceDiscoveryResult,
    SourceIngestionResult,
)
from harborrag_runtime.temporal.source_batch_workflow import SourceBatchWorkflow
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .fixtures import plan_reference as _plan_reference
from .fixtures import source_input as _source

DOCUMENT_COUNT = 10


class _LiveBatchHandle:
    """Runs a real `SourceBatchWorkflow` so forwarded pause/resume signals land on it.

    Unlike a stub that only records signal names, this lets the test verify the
    parent's pause actually reaches the in-flight batch child, not just that the
    parent's own flags flipped.
    """

    def __init__(self, batch: SourceBatchWorkflow, request) -> None:
        self._batch = batch
        self._task = asyncio.create_task(batch.run(request))

    def __await__(self):
        return self._task.__await__()

    async def signal(self, name: str) -> None:
        getattr(self._batch, name)()


async def _until(predicate, *, attempts: int = 10_000) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never satisfied")


@pytest.fixture(autouse=True)
def _fast_workflow_primitives(monkeypatch):
    # A real `wait_condition` needs a live Temporal workflow sandbox. This
    # stand-in polls cooperatively so pause/resume can be driven by the test
    # in real time instead of Temporal's replay clock. `source_workflow` and
    # `source_batch_workflow` both do `from temporalio import workflow`, so
    # patching the attribute once (via either import path) covers both.
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
async def test_pause_stops_new_dispatch_and_resume_completes_without_losing_progress(
    monkeypatch,
):
    plan = _plan_reference()
    # A single outer batch spanning all 10 documents, so the two document
    # waves below (0-7, then 8-9) are dispatched by one `SourceBatchWorkflow`
    # child rather than five separate ones -- this is what exercises pause
    # landing on a batch that is genuinely still running.
    source = replace(_source(), batch_size=DOCUMENT_COUNT)
    assert source.document_concurrency == 8  # first wave: docs 0-7, second wave: docs 8-9

    started: list[int] = []
    finished: list[int] = []
    wave_release = {0: asyncio.Event(), 1: asyncio.Event()}

    async def execute_activity(name, request, **options):
        del options
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=DOCUMENT_COUNT,
            )
        if name == "harborrag.cleanup_source_projections":
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

    async def execute_child_workflow(name, request, **options):
        assert name == "harborrag.document_ingestion"
        started.append(request.document_index)
        wave = request.document_index // source.document_concurrency
        await wave_release[wave].wait()
        finished.append(request.document_index)
        return DocumentIngestionOutcome.PUBLISHED

    async def start_child_workflow(name, request, **options):
        assert name == "harborrag.source_batch"
        return _LiveBatchHandle(SourceBatchWorkflow(), request)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        start_child_workflow,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
        execute_child_workflow,
    )

    instance = SourceIngestionWorkflow()
    run_task = asyncio.create_task(instance.run(source))

    # Let the first wave (documents 0-7) fully start before pausing, so the
    # pause lands while real work is genuinely in flight.
    await _until(lambda: len(started) == 8)

    instance.pause()

    # The paused state must be visible immediately, not only once the
    # in-flight batch happens to reach its own boundary.
    status = instance.get_status()
    assert status.status == "PAUSED"
    assert status.paused is True

    # Finish the in-flight wave; already-started work must not be discarded.
    wave_release[0].set()
    await _until(lambda: len(finished) == 8)

    # Give the paused signal every chance to (wrongly) let the next wave
    # start; it must not, since pause forbids starting *new* work.
    for _ in range(50):
        await asyncio.sleep(0)
    assert started == list(range(8))

    instance.resume()
    assert instance.get_status().status == "RUNNING"

    wave_release[1].set()
    result = await run_task

    assert sorted(started) == list(range(DOCUMENT_COUNT))
    assert sorted(finished) == list(range(DOCUMENT_COUNT))
    assert result.status == "COMPLETED"
    assert result.published == DOCUMENT_COUNT
    assert result.discovered == DOCUMENT_COUNT
    assert instance.get_status().paused is False
