from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.temporal.schemas import (
    ArtifactOutcomeCheckpoint,
    ArtifactReference,
    ArtifactResult,
    ArtifactStage,
    ArtifactStatus,
    ConcurrencyUpdate,
    DiscoveryResult,
    IngestionRunInput,
    PartitionResult,
    PendingResolution,
    ReconciliationResult,
    RunContinuationState,
    RunProgress,
    RunStatus,
    WorkflowOptions,
)
from harborrag_runtime.temporal.workflows.ingestion import IngestionRunWorkflow


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


def _input(**options) -> IngestionRunInput:
    return IngestionRunInput(
        run_id="run-1",
        tenant_id="tenant-1",
        connector_name="local",
        manifest_id="manifest-1",
        generation_id="generation-1",
        options=WorkflowOptions(**options),
    )


def _reference(index: int) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=f"artifact-{index}",
        source_ref=f"source://{index}",
        source_kind="local",
        connector_name="local",
    )


def _patch_common(monkeypatch, discovery: DiscoveryResult) -> None:
    async def execute(name, request, result_type, activity_class, options):
        return discovery

    async def reconcile(request, options):
        return ReconciliationResult("reconcile://run-1", RunStatus.COMPLETED)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.execute_activity",
        execute,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.execute_reconciliation",
        reconcile,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.wait_condition",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.sleep",
        AsyncMock(),
    )


@pytest.mark.asyncio
async def test_empty_discovery_completes_compact_run(monkeypatch) -> None:
    _patch_common(
        monkeypatch,
        DiscoveryResult((), None, "checkpoint://empty", True),
    )

    result = await IngestionRunWorkflow().run(_input())

    assert result.status is RunStatus.COMPLETED
    assert result.progress.discovered == 0
    assert result.reconciliation_ref == "reconcile://run-1"


@pytest.mark.asyncio
async def test_multiple_partitions_aggregate_partial_failure(monkeypatch) -> None:
    artifacts = tuple(_reference(index) for index in range(3))
    _patch_common(
        monkeypatch,
        DiscoveryResult(artifacts, None, "checkpoint://complete", True),
    )
    started = []

    async def start(name, request, **kwargs):
        started.append(request.partition_number)
        failed = request.partition_number == 1
        progress = RunProgress(
            processed=len(request.artifacts),
            succeeded=0 if failed else len(request.artifacts),
            failed=len(request.artifacts) if failed else 0,
        )
        return _Handle(
            PartitionResult(
                request.partition_number,
                progress,
                failed_artifacts=(request.artifacts[0].artifact_id,) if failed else (),
            )
        )

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.start_child_workflow",
        start,
    )

    workflow = IngestionRunWorkflow()
    result = await workflow.run(_input(partition_size=1, partition_concurrency=2))

    assert started == [0, 1, 2]
    assert result.progress.discovered == 3
    assert result.progress.processed == 3
    assert result.progress.partitions == 3
    assert workflow.get_failed_artifacts() == ("artifact-1",)


@pytest.mark.asyncio
async def test_graceful_cancellation_reconciles_without_cleanup(monkeypatch) -> None:
    _patch_common(
        monkeypatch,
        DiscoveryResult((), None, "checkpoint://empty", True),
    )
    workflow = IngestionRunWorkflow()
    workflow._cancel_requested = True

    result = await workflow.run(_input())

    assert result.status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_continue_as_new_rehydrates_operator_visible_state(monkeypatch) -> None:
    artifact = _reference(0)
    _patch_common(
        monkeypatch,
        DiscoveryResult((artifact,), "1", "checkpoint://page", False),
    )
    pending = PendingResolution(
        artifact_id=artifact.artifact_id,
        request_ref="resolution://artifact-0",
        reason="operator review",
        resume_stage=ArtifactStage.INDEX,
    )
    workflow = IngestionRunWorkflow()
    workflow.retry_failed(("artifact-prior",))

    async def start(name, request, **kwargs):
        await workflow.adjust_concurrency(ConcurrencyUpdate(2, 3))
        return _Handle(
            PartitionResult(
                request.partition_number,
                RunProgress(processed=1, failed=1),
                failed_artifacts=(artifact.artifact_id,),
                pending_resolutions=(pending,),
                artifact_results=(
                    ArtifactResult(
                        artifact_id=artifact.artifact_id,
                        status=ArtifactStatus.FAILED,
                        pending_resolution=pending,
                    ),
                ),
            )
        )

    class Continued(Exception):
        def __init__(self, value) -> None:
            self.value = value

    def continue_as_new(value):
        raise Continued(value)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.start_child_workflow",
        start,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.info",
        lambda: SimpleNamespace(is_continue_as_new_suggested=lambda: False),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.continue_as_new",
        continue_as_new,
    )

    with pytest.raises(Continued) as captured:
        await workflow.run(_input(continue_after_partitions=1))

    continuation = captured.value.value
    assert continuation.source_cursor == "1"
    assert continuation.next_partition == 1
    assert continuation.progress.processed == 1
    assert continuation.continuation.partition_concurrency == 2
    assert continuation.continuation.artifact_concurrency == 3
    assert continuation.continuation.retry_requested == ("artifact-prior",)
    assert continuation.continuation.pending_resolutions == (pending,)
    assert (
        continuation.continuation.artifact_outcomes[0].reference.artifact_id == artifact.artifact_id
    )

    _patch_common(
        monkeypatch,
        DiscoveryResult((), None, "checkpoint://complete", True),
    )
    resumed = IngestionRunWorkflow()
    await resumed.run(continuation)

    assert resumed.get_failed_artifacts() == (artifact.artifact_id,)
    assert resumed.get_pending_resolutions() == (pending,)
    assert resumed._retry_requested == ["artifact-prior"]
    assert resumed._partition_concurrency == 2
    assert resumed._artifact_concurrency == 3


@pytest.mark.asyncio
async def test_retry_from_prior_page_replaces_failed_outcome(monkeypatch) -> None:
    artifact = _reference(0)
    failed = ArtifactResult(
        artifact_id=artifact.artifact_id,
        status=ArtifactStatus.FAILED,
        error_type="provider_unavailable",
    )
    continuation = RunContinuationState(
        artifact_outcomes=(ArtifactOutcomeCheckpoint(artifact, failed),),
        retry_requested=(artifact.artifact_id,),
    )
    _patch_common(
        monkeypatch,
        DiscoveryResult((), None, "checkpoint://complete", True),
    )
    attempts: list[tuple[str, ...]] = []

    async def start(name, request, **kwargs):
        attempts.append(tuple(item.artifact_id for item in request.artifacts))
        return _Handle(
            PartitionResult(
                request.partition_number,
                RunProgress(processed=1, succeeded=1),
                artifact_results=(
                    ArtifactResult(
                        artifact_id=artifact.artifact_id,
                        status=ArtifactStatus.SUCCEEDED,
                    ),
                ),
            )
        )

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.start_child_workflow",
        start,
    )
    workflow = IngestionRunWorkflow()

    result = await workflow.run(
        IngestionRunInput(
            run_id="run-1",
            tenant_id="tenant-1",
            connector_name="local",
            manifest_id="manifest-1",
            generation_id="generation-1",
            progress=RunProgress(
                discovered=1,
                processed=1,
                failed=1,
                partitions=1,
            ),
            continuation=continuation,
        )
    )

    assert attempts == [(artifact.artifact_id,)]
    assert result.status is RunStatus.COMPLETED
    assert result.progress.processed == 1
    assert result.progress.failed == 0
    assert result.progress.succeeded == 1
    assert workflow.get_failed_artifacts() == ()
    assert workflow._retry_requested == []


@pytest.mark.asyncio
async def test_pause_resume_retry_and_concurrency_controls() -> None:
    workflow = IngestionRunWorkflow()
    workflow._run_id = "run-1"

    await workflow.pause()
    assert workflow.get_status().paused is True
    await workflow.resume()
    assert workflow.get_status().paused is False
    workflow.retry_failed(("artifact-1", "artifact-1"))
    assert workflow._retry_requested == ["artifact-1"]
    update = ConcurrencyUpdate(2, 3)
    assert await workflow.adjust_concurrency(update) == update
    assert workflow._partition_concurrency == 2
    assert workflow._artifact_concurrency == 3
