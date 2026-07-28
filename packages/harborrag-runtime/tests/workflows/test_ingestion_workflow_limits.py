from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
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
    def __init__(self, result: PartitionResult) -> None:
        self.result = result

    def __await__(self):
        async def value():
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
async def test_artifact_limit_bounds_discovery_and_finishes_without_next_page(
    monkeypatch,
) -> None:
    requested_page_sizes: list[int] = []
    discoveries = 0

    async def execute(name, request, result_type, activity_class, options):
        nonlocal discoveries
        discoveries += 1
        requested_page_sizes.append(request.page_size)
        return DiscoveryResult(
            (_reference(0), _reference(1)),
            "next-page",
            "checkpoint://limited",
            False,
        )

    async def reconcile(request, options):
        return ReconciliationResult("reconcile://run-1", RunStatus.COMPLETED)

    async def start(name, request, **kwargs):
        return _Handle(
            PartitionResult(
                request.partition_number,
                RunProgress(
                    processed=len(request.artifacts),
                    succeeded=len(request.artifacts),
                ),
            )
        )

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
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.ingestion.workflow.start_child_workflow",
        start,
    )
    request = IngestionRunInput(
        run_id="run-1",
        tenant_id="tenant-1",
        connector_name="local",
        manifest_id="manifest-1",
        generation_id="generation-1",
        options=WorkflowOptions(
            max_artifacts=2,
            partition_size=1,
            partition_concurrency=5,
        ),
    )

    result = await IngestionRunWorkflow().run(request)

    assert discoveries == 1
    assert requested_page_sizes == [2]
    assert result.progress.discovered == 2
    assert result.progress.processed == 2
