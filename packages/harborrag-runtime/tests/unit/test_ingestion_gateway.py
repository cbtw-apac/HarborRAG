"""The application contract stays independent of Temporal history payloads."""

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.converter import DataConverter

from harborrag_runtime.ingestion_contracts import (
    IngestionExecutionReference,
    IngestionExecutionResult,
    IngestionExecutionStatus,
    IngestionRetryRequest,
    PreparedSourceSubmission,
)
from harborrag_runtime.source_query import ProcessingProfileInput, SourceQuery
from harborrag_runtime.temporal.gateway import TemporalIngestionGateway, to_temporal_source
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import (
    RetryFailuresInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)


def _submission() -> PreparedSourceSubmission:
    return PreparedSourceSubmission(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="docs",
        connector_type="local",
        connection_id="connection-1",
        source_scope_id="scope-1",
        configuration_fingerprint="config-v1",
        processing=ProcessingProfileInput(
            "parser", "normalizer", "chunks", "dense", "sparse", "graph"
        ),
        query=SourceQuery(path="handbook", limit=7, filters_json='{"team":"platform"}'),
        document_concurrency=3,
        batch_size=20,
    )


@pytest.mark.asyncio
async def test_gateway_translates_submissions_and_retries_without_exposing_workflow_options():
    reference = RuntimeWorkflowRef("task-1", "workflow-1", "execution-1")
    client = SimpleNamespace(
        start_ingestion=AsyncMock(return_value=reference),
        start_retry_failures=AsyncMock(return_value=reference),
    )
    gateway = TemporalIngestionGateway(client)
    submission = _submission()

    actual = await gateway.start_ingestion(submission)

    assert type(actual) is IngestionExecutionReference
    assert asdict(actual) == asdict(reference)
    sent = client.start_ingestion.await_args.args[0]
    assert type(sent) is SourceIngestionInput
    assert asdict(sent) == {
        **asdict(submission),
        "continuation": None,
        "workflow_options": asdict(sent.workflow_options),
    }
    assert not hasattr(submission, "workflow_options")

    retry = IngestionRetryRequest("retry-1", "task-1", "tenant-1", ("document-1",), 3)
    await gateway.start_retry_failures(retry)
    sent_retry = client.start_retry_failures.await_args.args[0]
    assert type(sent_retry) is RetryFailuresInput
    assert asdict(sent_retry) == {
        **asdict(retry),
        "workflow_options": asdict(sent_retry.workflow_options),
    }


@pytest.mark.asyncio
async def test_gateway_translates_status_and_result_while_preserving_response_fields():
    status = SourceIngestionStatus("task-1", "RUNNING", False, False)
    result = SourceIngestionResult("task-1", "scan-1", 5, 3, 1, 1, ("removed",), 2, "PARTIAL")
    client = SimpleNamespace(
        get_status=AsyncMock(return_value=status), result=AsyncMock(return_value=result)
    )
    gateway = TemporalIngestionGateway(client)

    actual_status = await gateway.get_status("task-1")
    actual_result = await gateway.result("task-1")

    assert type(actual_status) is IngestionExecutionStatus
    assert asdict(actual_status) == asdict(status)
    assert type(actual_result) is IngestionExecutionResult
    assert asdict(actual_result) == asdict(result)


@pytest.mark.asyncio
async def test_gateway_delegates_control_progress_and_health():
    client = SimpleNamespace(
        pause=AsyncMock(),
        resume=AsyncMock(),
        cancel=AsyncMock(),
        get_progress=AsyncMock(return_value={"published": 3}),
        execution_status=AsyncMock(return_value="running"),
        health=AsyncMock(return_value=True),
    )
    gateway = TemporalIngestionGateway(client)

    await gateway.pause("task-1")
    await gateway.resume("task-1")
    await gateway.cancel("task-1")
    assert await gateway.get_progress("task-1") == {"published": 3}
    assert await gateway.execution_status("task-1") == "running"
    assert await gateway.health() is True

    for operation in (
        client.pause,
        client.resume,
        client.cancel,
        client.get_progress,
        client.execution_status,
    ):
        operation.assert_awaited_once_with("task-1")
    client.health.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_temporal_history_payload_still_decodes_with_neutral_base_fields():
    source = to_temporal_source(_submission())
    converter = DataConverter.default

    payloads = await converter.encode([asdict(source)])
    restored = await converter.decode(payloads, [SourceIngestionInput])

    assert restored == [source]
    assert isinstance(restored[0].processing, ProcessingProfileInput)
    assert isinstance(restored[0].query, SourceQuery)
