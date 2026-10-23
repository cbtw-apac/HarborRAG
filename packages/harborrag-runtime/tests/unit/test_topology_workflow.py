"""Separate deterministic workflow and durable redispatch behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio import workflow
from temporalio.exceptions import WorkflowAlreadyStartedError

from harborrag_runtime.topology.summary_workflow import SummaryProjectionWorkflow
from harborrag_runtime.topology.worker import dispatch
from harborrag_runtime.topology.workflow import TopologyEnrichmentWorkflow


@pytest.mark.asyncio
async def test_topology_workflow_prepares_inside_temporal_sandbox(sandbox_runner):
    sandbox_runner().prepare_workflow(
        workflow._Definition.must_from_class(TopologyEnrichmentWorkflow)
    )


@pytest.mark.asyncio
async def test_summary_workflow_prepares_inside_temporal_sandbox(sandbox_runner):
    sandbox_runner().prepare_workflow(
        workflow._Definition.must_from_class(SummaryProjectionWorkflow)
    )


@pytest.mark.asyncio
async def test_dispatch_does_not_duplicate_running_workflows():
    repo = SimpleNamespace(
        reconcile=AsyncMock(),
        runnable_jobs=AsyncMock(return_value=(SimpleNamespace(job_id="job-a", attempts=0),)),
    )
    client = SimpleNamespace(start_workflow=AsyncMock())
    settings = SimpleNamespace(topology_job_seconds=300, topology_task_queue="model-queue")
    assert await dispatch(client, repo, settings, "tenant-a") == 1
    call = client.start_workflow.call_args
    assert call.kwargs["id"] == "harborrag-topology:job-a:0"
    assert call.kwargs["task_queue"] == "model-queue"
    assert call.args[1].tenant_id == "tenant-a"
    client.start_workflow.side_effect = WorkflowAlreadyStartedError("job-a", "topology")
    assert await dispatch(client, repo, settings, "tenant-a") == 0


@pytest.mark.asyncio
async def test_expired_attempt_dispatch_uses_next_fenced_attempt_identity():
    repo = SimpleNamespace(
        reconcile=AsyncMock(),
        runnable_jobs=AsyncMock(return_value=(SimpleNamespace(job_id="job-a", attempts=2),)),
    )
    client = SimpleNamespace(start_workflow=AsyncMock())
    settings = SimpleNamespace(topology_job_seconds=300, topology_task_queue="model-queue")
    await dispatch(client, repo, settings, "tenant-a")
    assert client.start_workflow.call_args.kwargs["id"].endswith(":2")
