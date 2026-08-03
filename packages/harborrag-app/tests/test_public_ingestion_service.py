"""Durable application-service tests for the public ingestion contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_app.workflow_control.errors import (
    IngestionAlreadyCompletedError,
    IngestionIdempotencyConflictError,
)
from harborrag_app.workflow_control.ingestion_models import IngestionCreateCommand
from harborrag_app.workflow_control.ingestion_presenters import task_id as generate_task_id
from harborrag_app.workflow_control.ingestion_service import IngestionApplicationService
from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_core.ingestion import IngestionTaskState, TaskDocumentResult
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import WorkflowSubmissionError
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    RetryFailuresInput,
    SourceIngestionInput,
)
from harborrag_runtime.temporal.submission import SourceSubmission
from harborrag_runtime.temporal.task_registry import IngestionTaskRegistry


class FakeTemporalClient:
    def __init__(self) -> None:
        self.started: list[SourceIngestionInput] = []
        self.cancelled: list[str] = []
        self.retries: list[RetryFailuresInput] = []

    async def start_ingestion(self, source: SourceIngestionInput) -> RuntimeWorkflowRef:
        self.started.append(source)
        return RuntimeWorkflowRef(source.task_id, "internal", "internal-run")

    async def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)

    async def start_retry_failures(
        self,
        request: RetryFailuresInput,
    ) -> RuntimeWorkflowRef:
        self.retries.append(request)
        return RuntimeWorkflowRef(request.retry_task_id, "internal", "internal-run")


def _source_input(
    _settings: RuntimeSettings,
    submission: SourceSubmission,
) -> SourceIngestionInput:
    task_id = submission.task_id
    return SourceIngestionInput(
        task_id=task_id,
        tenant_id=submission.tenant_id,
        connector_name="harborrag-workspace",
        connector_type="local",
        connection_id="harborrag-workspace",
        source_scope_id="scope-harborrag-workspace",
        configuration_fingerprint="connector-test",
        processing=ProcessingProfileInput(
            parser_profile="parser-v1",
            normalizer_version="normalizer-v1",
            chunk_strategy="chunk-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="sparse-v1",
            graph_projection_version="graph-v1",
        ),
    )


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


@pytest_asyncio.fixture
async def service_resources(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[IngestionApplicationService, IngestionControlPlaneDatabase, FakeTemporalClient]
]:
    client = SQLAlchemyDBClient(
        backend="sqlite",
        url=f"sqlite+aiosqlite:///{tmp_path / 'ingestion-api.db'}",
        pool_size=None,
        max_overflow=None,
        pool_recycle_seconds=1_800,
        echo=False,
    )
    control = IngestionControlPlaneDatabase(client, create_schema=True)
    await control.connect()
    registry = IngestionTaskRegistry(control)
    temporal = FakeTemporalClient()
    identifiers = iter(
        (
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000004",
        )
    )

    async def runtime_client() -> FakeTemporalClient:
        return temporal

    async def task_store() -> IngestionTaskRegistry:
        return registry

    service = IngestionApplicationService(
        RuntimeSettings(ingestion_tenant_id="default"),
        client_provider=runtime_client,
        task_store_provider=task_store,
        source_input_builder=_source_input,
        task_id_factory=lambda: next(identifiers),
    )
    try:
        yield service, control, temporal
    finally:
        await control.close()


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
