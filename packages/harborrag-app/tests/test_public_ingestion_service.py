"""Durable application-service tests for the public ingestion contract."""

from __future__ import annotations

from uuid import UUID

import pytest
from workflow_control_fixtures import (
    FakeTemporalClient,
)
from workflow_control_fixtures import (
    public_ingestion_command as _command,
)
from workflow_control_fixtures import (
    service_resources as service_resources,
)

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_app.workflow_control.errors import (
    IngestionAlreadyCompletedError,
    IngestionAlreadyPausedError,
    IngestionAlreadyRunningError,
    IngestionIdempotencyConflictError,
)
from harborrag_app.workflow_control.ingestion.presenters import task_id as generate_task_id
from harborrag_app.workflow_control.ingestion.service import IngestionApplicationService
from harborrag_core.contracts.errors import HarborConflictError, HarborConnectionError
from harborrag_core.ingestion import IngestionTaskState, TaskDocumentResult
from harborrag_runtime.errors import WorkflowOperationError, WorkflowSubmissionError
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import SourceIngestionInput


def test_generated_task_id_is_uuid4() -> None:
    parsed = UUID(generate_task_id())
    assert parsed.version == 4
    assert parsed.variant == "specified in RFC 4122"


@pytest.mark.asyncio
async def test_idempotency_is_durable_and_submits_temporal_once(
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
    service, _control, temporal = service_resources

    first = await service.submit(_command(), idempotency_key="local-001")
    second = await service.submit(_command(), idempotency_key="local-001")

    assert first["task_id"] == second["task_id"]
    assert len(temporal.started) == 1
    with pytest.raises(IngestionIdempotencyConflictError):
        await service.submit(_command(marker="different"), idempotency_key="local-001")


@pytest.mark.asyncio
async def test_pending_registration_retries_ambiguous_temporal_submission(
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
    service, control, temporal = service_resources
    original_start = temporal.start_ingestion
    attempts = 0

    async def flaky_start(source: SourceIngestionInput) -> RuntimeWorkflowRef:
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
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
    service, control, temporal = service_resources
    original_start = temporal.start_ingestion
    attempts = 0

    async def flaky_start(source: SourceIngestionInput) -> RuntimeWorkflowRef:
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
async def test_status_and_cursor_pages_are_read_from_the_database(
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)
    await control.tasks.update_summary(
        task_id,
        {"stage": "PROCESSING_DOCUMENTS", "discovered": 3, "admitted": 3},
    )
    for index in range(3):
        await control.tasks.record_document_result(
            TaskDocumentResult(
                task_id=task_id,
                document_id=f"document:{index}",
                status="published",
                result={
                    "source_item_id": f"adr/{index}.md",
                    "document_kind": "file",
                    "title": f"ADR-{index}",
                },
            )
        )

    task = await service.get_task(task_id)
    first = await service.list_documents(
        task_id=task_id,
        status="SUCCESS",
        cursor=None,
        limit=2,
    )
    second = await service.list_documents(
        task_id=task_id,
        status="SUCCESS",
        cursor=str(first["next_cursor"]),
        limit=2,
    )

    assert task["progress"] == {
        "discovered": 3,
        "admitted": 3,
        "processed": 3,
        "succeeded": 3,
        "failed": 0,
        "skipped": 0,
        "removed": 0,
    }
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert len(temporal.started) == 1


@pytest.mark.asyncio
async def test_cancel_and_retry_apply_postgres_state_rules(
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
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
    with pytest.raises(IngestionAlreadyCompletedError):
        await service.pause(task_id)
    with pytest.raises(IngestionAlreadyCompletedError):
        await service.resume(task_id)


@pytest.mark.asyncio
async def test_pause_and_resume_forward_to_temporal(
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)

    paused = await service.pause(task_id)
    assert temporal.paused == [task_id]
    assert paused["status"] == "PENDING"
    assert paused["message"] == "Pause requested"

    # The Temporal activity settles the durable lifecycle asynchronously.
    await control.tasks.transition(task_id, IngestionTaskState.PAUSED)
    assert (await control.tasks.get(task_id)).status is IngestionTaskState.PAUSED

    with pytest.raises(IngestionAlreadyPausedError, match="already paused"):
        await service.pause(task_id)
    assert temporal.paused == [task_id]

    with pytest.raises(HarborConflictError, match="finalization from PAUSED"):
        await control.tasks.finalize(
            task_id,
            IngestionTaskState.COMPLETED,
            summary={"stage": "COMPLETED"},
        )

    resumed = await service.resume(task_id)
    assert temporal.resumed == [task_id]
    assert resumed["status"] == "RUNNING"
    assert resumed["message"] == "Resume requested"
    # The Temporal activity settles the durable lifecycle asynchronously.
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)
    assert (await control.tasks.get(task_id)).status is IngestionTaskState.RUNNING


@pytest.mark.asyncio
async def test_resume_on_an_already_running_task_is_rejected(
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)

    with pytest.raises(IngestionAlreadyRunningError, match="already running"):
        await service.resume(task_id)
    assert temporal.resumed == []


@pytest.mark.asyncio
async def test_failed_pause_and_resume_do_not_change_durable_state(
    service_resources: tuple[
        IngestionApplicationService,
        IngestionControlPlaneDatabase,
        FakeTemporalClient,
    ],
) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)

    async def fail_pause(_task_id: str) -> None:
        raise WorkflowOperationError("pause failed")

    temporal.pause = fail_pause  # type: ignore[method-assign]
    with pytest.raises(HarborConnectionError):
        await service.pause(task_id)
    assert (await control.tasks.get(task_id)).status is IngestionTaskState.RUNNING

    await control.tasks.transition(task_id, IngestionTaskState.PAUSED)

    async def fail_resume(_task_id: str) -> None:
        raise WorkflowOperationError("resume failed")

    temporal.resume = fail_resume  # type: ignore[method-assign]
    with pytest.raises(HarborConnectionError):
        await service.resume(task_id)
    assert (await control.tasks.get(task_id)).status is IngestionTaskState.PAUSED
