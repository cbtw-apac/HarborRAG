"""Durable application-service tests for the public ingestion contract."""

from __future__ import annotations

from uuid import UUID

import pytest

from harborrag_app.workflow_control.errors import (
    IngestionAlreadyCompletedError,
    IngestionIdempotencyConflictError,
)
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_app.workflow_control.ingestion.presenters import task_id as generate_task_id
from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_core.ingestion import IngestionTaskState, TaskDocumentResult
from harborrag_runtime.errors import WorkflowSubmissionError


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


def test_generated_task_id_is_uuid4() -> None:
    parsed = UUID(generate_task_id())
    assert parsed.version == 4
    assert parsed.variant == "specified in RFC 4122"


@pytest.mark.asyncio
async def test_idempotency_is_durable_and_submits_temporal_once(service_resources) -> None:
    service, _control, temporal = service_resources

    first = await service.submit(_command(), idempotency_key="local-001")
    second = await service.submit(_command(), idempotency_key="local-001")

    assert first["task_id"] == second["task_id"]
    assert len(temporal.started) == 1
    with pytest.raises(IngestionIdempotencyConflictError):
        await service.submit(_command(marker="different"), idempotency_key="local-001")


@pytest.mark.asyncio
async def test_pending_registration_retries_ambiguous_temporal_submission(
    service_resources,
) -> None:
    service, control, temporal = service_resources
    original_start = temporal.start_ingestion
    attempts = 0

    async def flaky_start(source):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WorkflowSubmissionError("temporarily unavailable")
        return await original_start(source)

    temporal.start_ingestion = flaky_start  # type: ignore[method-assign]

    with pytest.raises(HarborConnectionError):
        await service.submit(_command(), idempotency_key="retryable-submit")

    pending = await control.tasks.get("00000000-0000-4000-8000-000000000001")
    assert pending is not None
    assert pending.status == IngestionTaskState.PENDING
    assert pending.summary.get("submission_state") is None

    accepted = await service.submit(_command(), idempotency_key="retryable-submit")

    assert accepted["task_id"] == pending.task_id
    assert [source.task_id for source in temporal.started] == [pending.task_id]
    submitted = await control.tasks.get(pending.task_id)
    assert submitted is not None
    assert submitted.summary["submission_state"] == "submitted"


@pytest.mark.asyncio
async def test_recovery_submits_pending_task_without_client_idempotency_key(
    service_resources,
) -> None:
    service, control, temporal = service_resources
    original_start = temporal.start_ingestion
    attempts = 0

    async def flaky_start(source):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WorkflowSubmissionError("temporarily unavailable")
        return await original_start(source)

    temporal.start_ingestion = flaky_start  # type: ignore[method-assign]

    with pytest.raises(HarborConnectionError):
        await service.submit(_command(), idempotency_key=None)

    recovered = await service.recover_pending_submissions()
    pending = await control.tasks.get("00000000-0000-4000-8000-000000000001")

    assert recovered == 1
    assert pending is not None
    assert pending.summary["submission_state"] == "submitted"
    assert [source.task_id for source in temporal.started] == [pending.task_id]


@pytest.mark.asyncio
async def test_cancel_and_retry_apply_postgres_state_rules(service_resources) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    await service.cancel(task_id)
    assert temporal.cancelled == [task_id]

    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)
    await control.tasks.record_document_result(
        TaskDocumentResult(
            task_id=task_id,
            document_id="document:retryable",
            status="failed",
            result={
                "safe_error_code": "vector_write_failed",
                "failure_stage": "WriteVectorProjection",
                "retryable": True,
            },
        )
    )
    await control.tasks.record_document_result(
        TaskDocumentResult(
            task_id=task_id,
            document_id="document:permanent",
            status="failed",
            result={
                "safe_error_code": "chunk_invalid",
                "failure_stage": "ChunkAndValidate",
                "retryable": False,
            },
        )
    )
    await control.tasks.finalize(
        task_id,
        IngestionTaskState.FAILED,
        summary={"stage": "COMPLETED", "failed": 2},
    )

    retry = await service.retry_failures(task_id=task_id, document_ids=[])

    assert retry["accepted_document_count"] == 1
    assert temporal.retries[0].tenant_id == "ACME"
    assert temporal.retries[0].document_ids == ("document:retryable",)
    with pytest.raises(IngestionAlreadyCompletedError):
        await service.cancel(task_id)
