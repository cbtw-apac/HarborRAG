"""Reconciliation-focused tests for ingestion status reads."""

from __future__ import annotations

from datetime import timedelta

import pytest

import harborrag_app.workflow_control.ingestion.status_reconciliation as status_reconciliation
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.errors import RuntimeConnectionError, WorkflowOperationError


def _command(*, marker: str = "one") -> IngestionCreateCommand:
    return IngestionCreateCommand(
        tenant_id="ACME",
        connection_id="harborrag-workspace",
        force_reprocess=False,
        public_request={
            "connection_id": "harborrag-workspace",
            "tenant": "ACME",
            "marker": marker,
        },
    )


@pytest.mark.asyncio
async def test_get_task_reconciles_temporal_failed_execution_to_terminal_failure(
    service_resources,
) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    temporal.execution_state_by_task[task_id] = "failed"

    response = await service.get_task(task_id)
    persisted = await control.tasks.get(task_id)

    assert response["status"] == "FAILED"
    assert response["stage"] == "COMPLETED"
    assert persisted is not None
    assert persisted.status == IngestionTaskState.FAILED
    assert persisted.summary["failed_stage"] == "workflow_execution"
    assert persisted.summary["error_code"] == "execution_failed"


@pytest.mark.asyncio
async def test_get_task_marks_stale_queued_running_as_failed_and_terminates_workflow(
    monkeypatch,
    service_resources,
) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    temporal.execution_state_by_task[task_id] = "running"
    monkeypatch.setattr(
        status_reconciliation,
        "_STALE_QUEUED_RUNNING_TIMEOUT",
        timedelta(seconds=0),
    )

    response = await service.get_task(task_id)
    persisted = await control.tasks.get(task_id)

    assert response["status"] == "FAILED"
    assert response["stage"] == "COMPLETED"
    assert temporal.terminated == [task_id]
    assert persisted is not None
    assert persisted.status == IngestionTaskState.FAILED
    assert persisted.summary["failed_stage"] == "workflow_dispatch"
    assert persisted.summary["error_code"] == "worker_unavailable"


@pytest.mark.asyncio
async def test_get_task_keeps_stale_queued_running_active_when_termination_fails(
    monkeypatch,
    service_resources,
) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    temporal.execution_state_by_task[task_id] = "running"
    monkeypatch.setattr(
        status_reconciliation,
        "_STALE_QUEUED_RUNNING_TIMEOUT",
        timedelta(seconds=0),
    )

    async def _terminate_fails(_task_id: str, *, reason: str) -> None:
        del reason
        raise WorkflowOperationError("terminate failed")

    monkeypatch.setattr(temporal, "terminate", _terminate_fails)

    response = await service.get_task(task_id)
    persisted = await control.tasks.get(task_id)

    assert response["status"] == "PENDING"
    assert response["stage"] == "QUEUED"
    assert persisted is not None
    assert persisted.status == IngestionTaskState.PENDING


@pytest.mark.asyncio
async def test_get_task_returns_persisted_task_when_runtime_client_is_unavailable(
    monkeypatch,
    service_resources,
) -> None:
    service, control, _temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])

    async def _failing_client_provider() -> object:
        raise RuntimeConnectionError("Temporal unavailable")

    monkeypatch.setattr(
        service._status_reconciler,
        "_client_provider",
        _failing_client_provider,
    )

    response = await service.get_task(task_id)
    persisted = await control.tasks.get(task_id)

    assert response["task_id"] == task_id
    assert response["status"] == "PENDING"
    assert response["stage"] == "QUEUED"
    assert persisted is not None
    assert persisted.status == IngestionTaskState.PENDING
