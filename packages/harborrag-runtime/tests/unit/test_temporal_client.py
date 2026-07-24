from __future__ import annotations

import pytest
from temporalio.service import RPCError, RPCStatusCode

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import WorkflowOperationError
from harborrag_runtime.temporal.client import TemporalRuntimeClient
from harborrag_runtime.temporal.identity import ingestion_workflow_id
from harborrag_runtime.temporal.schemas import (
    ArtifactStage,
    IngestionRunInput,
    ResolutionDecision,
    ResolutionReceipt,
    RunProgress,
    RunStatus,
    WorkflowStatusView,
)


class _Handle:
    first_execution_run_id = "temporal-run-1"

    def __init__(self) -> None:
        self.signals = []
        self.updates = []
        self.queries = []
        self.cancelled = False
        self.query_result = WorkflowStatusView(
            run_id="run-1",
            status=RunStatus.RUNNING,
            progress=RunProgress(),
            current_partition=None,
            paused=False,
            cancel_requested=False,
        )

    async def signal(self, name, arg=None):
        self.signals.append((name, arg))

    async def query(self, name, **kwargs):
        self.queries.append(name)
        return self.query_result

    async def execute_update(self, name, arg, **kwargs):
        self.updates.append((name, arg))
        return ResolutionReceipt(arg.artifact_id, "decision://1", True, ArtifactStage.FETCH)

    async def cancel(self):
        self.cancelled = True


class _Client:
    def __init__(self) -> None:
        self.handle = _Handle()
        self.started = None
        self.service_client = _ServiceClient()

    async def start_workflow(self, name, request, **kwargs):
        self.started = (name, request, kwargs)
        return self.handle

    def get_workflow_handle(self, workflow_id, **kwargs):
        return self.handle


class _ServiceClient:
    async def check_health(self) -> bool:
        return True


def _input() -> IngestionRunInput:
    return IngestionRunInput(
        run_id="run-1",
        tenant_id="tenant-1",
        connector_name="local",
        manifest_id="manifest-1",
        generation_id="generation-1",
    )


@pytest.mark.asyncio
async def test_runtime_client_starts_queries_signals_and_cancels() -> None:
    sdk = _Client()
    client = TemporalRuntimeClient(sdk, TemporalRuntimeConfig())

    reference = await client.start_ingestion(_input())
    assert await client.health() is True
    status = await client.get_status("run-1")
    await client.pause("run-1")
    await client.resume("run-1")
    await client.retry_failed("run-1", ("artifact-1",))
    await client.cancel("run-1", graceful=False)

    assert reference.workflow_id == ingestion_workflow_id("run-1")
    assert status.status is RunStatus.RUNNING
    assert sdk.handle.signals == [
        ("pause", None),
        ("resume", None),
        ("retry_failed", ("artifact-1",)),
    ]
    assert sdk.handle.cancelled is True


@pytest.mark.asyncio
async def test_runtime_client_submits_resolution_to_artifact_workflow() -> None:
    sdk = _Client()
    client = TemporalRuntimeClient(sdk, TemporalRuntimeConfig())
    decision = ResolutionDecision(
        artifact_id="artifact-1",
        request_ref="resolution://1",
        decision="accept",
        actor_id="member-1",
        submitted_at="2026-07-22T10:00:00Z",
    )
    receipt = await client.submit_resolution("run-1", decision)

    assert receipt.accepted is True
    assert sdk.handle.updates[0][0] == "submit_resolution"


@pytest.mark.asyncio
async def test_runtime_client_wraps_missing_workflow_error() -> None:
    sdk = _Client()

    async def missing(name, **kwargs):
        raise RPCError("missing", RPCStatusCode.NOT_FOUND, b"")

    sdk.handle.query = missing
    client = TemporalRuntimeClient(sdk, TemporalRuntimeConfig())

    with pytest.raises(WorkflowOperationError) as captured:
        await client.get_status("missing")

    assert isinstance(captured.value.__cause__, RPCError)
