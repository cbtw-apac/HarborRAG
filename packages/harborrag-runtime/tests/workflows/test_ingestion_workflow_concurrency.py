from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
    ConcurrencyUpdate,
    DiscoveryResult,
    IngestionRunInput,
    PartitionResult,
    ReconciliationResult,
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
@pytest.mark.asyncio
async def test_concurrency_update_does_not_skip_partitions(monkeypatch) -> None:
    artifacts = tuple(_reference(index) for index in range(4))
    _patch_common(
        monkeypatch,
        DiscoveryResult(artifacts, None, "checkpoint://complete", True),
    )
    workflow = IngestionRunWorkflow()
    started: list[int] = []

    async def start(name, request, **kwargs):
        started.append(request.partition_number)
        if request.partition_number == 0:
            await workflow.adjust_concurrency(ConcurrencyUpdate(1, 1))
        return _Handle(
            PartitionResult(
                request.partition_number,
                RunProgress(processed=1, succeeded=1),
            )
        )

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.start_child_workflow",
        start,
    )

    result = await workflow.run(_input(partition_size=1, partition_concurrency=2))

    assert started == [0, 1, 2, 3]
    assert result.progress.processed == 4
