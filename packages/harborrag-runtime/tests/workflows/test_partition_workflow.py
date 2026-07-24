from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ChildWorkflowError

from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
    ArtifactResult,
    ArtifactStatus,
    ConcurrencyUpdate,
    PartitionInput,
    WorkflowOptions,
)
from harborrag_runtime.temporal.workflows.partition import IngestionPartitionWorkflow


@pytest.fixture(autouse=True)
def _enable_rolling_artifact_pool(monkeypatch) -> None:
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.patched",
        lambda _patch_id: True,
    )


class _Handle:
    def __init__(self, result) -> None:
        self.result = result
        self.signals = []

    def __await__(self):
        async def value():
            return self.result

        return value().__await__()

    async def signal(self, name, arg=None) -> None:
        self.signals.append((name, arg))


class _FailedHandle:
    def __await__(self):
        async def failed():
            raise ChildWorkflowError(
                "child failed",
                namespace="default",
                workflow_id="artifact-child",
                run_id="run-1",
                workflow_type="harborrag.artifact_ingestion",
                initiated_event_id=1,
                started_event_id=2,
                retry_state=None,
            )

        return failed().__await__()


class _BlockedHandle:
    def __init__(self, result: ArtifactResult) -> None:
        self.result = result
        self.release = asyncio.Event()

    def __await__(self):
        async def value():
            await self.release.wait()
            return self.result

        return value().__await__()


def _reference(index: int) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=f"artifact-{index}",
        source_ref=f"source://{index}",
        source_kind="local",
        connector_name="local",
    )


@pytest.mark.asyncio
async def test_partition_bounds_artifact_concurrency_and_isolates_failure(monkeypatch) -> None:
    references = tuple(_reference(index) for index in range(3))
    started = []

    async def start(name, request, **kwargs):
        started.append(request.artifact.artifact_id)
        status = (
            ArtifactStatus.FAILED
            if request.artifact.artifact_id == "artifact-1"
            else ArtifactStatus.SUCCEEDED
        )
        return _Handle(ArtifactResult(request.artifact.artifact_id, status))

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.start_child_workflow",
        start,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.wait_condition",
        AsyncMock(),
    )
    options = WorkflowOptions(artifact_concurrency=2)
    request = PartitionInput(
        "run-1",
        "tenant-1",
        "manifest-1",
        0,
        references,
        "generation-1",
        options,
    )

    result = await IngestionPartitionWorkflow().run(request)

    assert started == ["artifact-0", "artifact-1", "artifact-2"]
    assert result.progress.processed == 3
    assert result.progress.succeeded == 2
    assert result.failed_artifacts == ("artifact-1",)


@pytest.mark.asyncio
async def test_partition_refills_each_available_slot_without_a_batch_barrier(
    monkeypatch,
) -> None:
    references = tuple(_reference(index) for index in range(3))
    handles: list[_BlockedHandle] = []
    started: list[str] = []

    async def start(name, request, **kwargs):
        started.append(request.artifact.artifact_id)
        handle = _BlockedHandle(
            ArtifactResult(request.artifact.artifact_id, ArtifactStatus.SUCCEEDED)
        )
        handles.append(handle)
        return handle

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.start_child_workflow",
        start,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.wait_condition",
        AsyncMock(),
    )
    request = PartitionInput(
        "run-1",
        "tenant-1",
        "manifest-1",
        0,
        references,
        "generation-1",
        WorkflowOptions(artifact_concurrency=2),
    )

    run = asyncio.create_task(IngestionPartitionWorkflow().run(request))
    await asyncio.sleep(0)
    assert started == ["artifact-0", "artifact-1"]

    handles[0].release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if len(started) == 3:
            break

    assert started == ["artifact-0", "artifact-1", "artifact-2"]
    assert not run.done()

    for handle in handles:
        handle.release.set()
    result = await run
    assert result.progress.succeeded == 3


def test_partition_pause_resume_and_graceful_cancel_state() -> None:
    workflow = IngestionPartitionWorkflow()

    workflow.pause()
    assert workflow._paused is True
    workflow.resume()
    assert workflow._paused is False
    workflow.request_graceful_cancel()
    assert workflow._cancel_requested is True


@pytest.mark.asyncio
async def test_child_workflow_failure_becomes_isolated_artifact_failure() -> None:
    reference = _reference(1)

    result = await IngestionPartitionWorkflow._artifact_result(
        _FailedHandle(),
        reference,
    )

    assert result.status is ArtifactStatus.FAILED
    assert result.artifact_id == reference.artifact_id


@pytest.mark.asyncio
async def test_concurrency_update_does_not_skip_artifacts(monkeypatch) -> None:
    references = tuple(_reference(index) for index in range(4))
    workflow = IngestionPartitionWorkflow()
    started: list[str] = []

    async def start(name, request, **kwargs):
        started.append(request.artifact.artifact_id)
        if request.artifact.artifact_id == "artifact-0":
            workflow.adjust_concurrency(ConcurrencyUpdate(1, 1))
        return _Handle(ArtifactResult(request.artifact.artifact_id, ArtifactStatus.SUCCEEDED))

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.start_child_workflow",
        start,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.wait_condition",
        AsyncMock(),
    )
    request = PartitionInput(
        "run-1",
        "tenant-1",
        "manifest-1",
        0,
        references,
        "generation-1",
        WorkflowOptions(artifact_concurrency=2),
    )

    result = await workflow.run(request)

    assert started == ["artifact-0", "artifact-1", "artifact-2", "artifact-3"]
    assert result.progress.processed == 4


@pytest.mark.asyncio
async def test_graceful_cancel_counts_unstarted_artifacts(monkeypatch) -> None:
    references = tuple(_reference(index) for index in range(3))
    workflow = IngestionPartitionWorkflow()
    workflow.request_graceful_cancel()
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.partition.workflow.wait_condition",
        AsyncMock(),
    )
    request = PartitionInput(
        "run-1",
        "tenant-1",
        "manifest-1",
        0,
        references,
        "generation-1",
        WorkflowOptions(artifact_concurrency=2),
    )

    result = await workflow.run(request)

    assert result.progress.processed == 3
    assert result.progress.cancelled == 3
