"""Durable ingestion client behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_runtime.config.temporal import (
    TemporalConnectionConfig,
    TemporalRuntimeConfig,
    TemporalTLSConfig,
)
from harborrag_runtime.temporal import client as client_module
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.maintenance_schemas import (
    ReindexInput,
    ReindexResult,
)
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)


def _processing() -> ProcessingProfileInput:
    return ProcessingProfileInput(
        parser_profile="parser-v1",
        normalizer_version="canonical-v1",
        chunk_strategy="chunks-v1",
        dense_encoder_profile="dense-v1",
        sparse_encoder_profile="sparse-v1",
        graph_projection_version="graph-v1",
        vector_projection_schema="vector-v2",
    )


def _source() -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        connector_type="local",
        connection_id="local-docs",
        source_scope_id="docs",
        configuration_fingerprint="config-v1",
        processing=_processing(),
    )


def _source_result() -> SourceIngestionResult:
    return SourceIngestionResult(
        task_id="task-1",
        scan_id="scan-1",
        discovered=1,
        published=1,
        unchanged=0,
        failed=0,
        removal_candidates=(),
        unresolved_relations=0,
    )


def _reindex() -> ReindexInput:
    return ReindexInput(
        reindex_job_id="reindex-1",
        tenant_id="tenant-1",
        processing=_processing(),
    )


def _reindex_result() -> ReindexResult:
    return ReindexResult(
        reindex_job_id="reindex-1",
        status="COMPLETED",
        connector_call_count=0,
        scanned_count=1,
        processed_count=1,
        published_count=1,
        skipped_count=0,
        failure_count=0,
    )


class _Handle:
    def __init__(self, result: object, *, status: object | None) -> None:
        self.first_execution_run_id = "execution-1"
        self.result = AsyncMock(return_value=result)
        self.query = AsyncMock(side_effect=self._query)
        self.describe = AsyncMock(return_value=SimpleNamespace(status=status))
        self.signal = AsyncMock()
        self.cancel = AsyncMock()

    @staticmethod
    def _query(name: str, **_options: object) -> object:
        if name == "get_status":
            return SourceIngestionStatus(
                task_id="task-1",
                status="RUNNING",
                paused=False,
                cancel_requested=False,
            )
        return {"published": 1}


class _SdkClient:
    def __init__(self) -> None:
        self.source_handle = _Handle(
            _source_result(),
            status=SimpleNamespace(name="COMPLETED"),
        )
        self.reindex_handle = _Handle(_reindex_result(), status=None)
        self.start_workflow = AsyncMock(side_effect=self._start)
        self.get_workflow_handle = Mock(side_effect=self._handle)
        self.service_client = SimpleNamespace(check_health=AsyncMock(return_value=True))

    def _start(self, workflow_name: str, *_args: object, **_kwargs: object) -> _Handle:
        return self.reindex_handle if workflow_name == "harborrag.reindex" else self.source_handle

    def _handle(self, workflow_id: str, **_kwargs: object) -> _Handle:
        return (
            self.reindex_handle
            if workflow_id.startswith("harborrag-reindex:")
            else self.source_handle
        )


@pytest.mark.asyncio
async def test_client_connects_with_plaintext_and_tls(monkeypatch) -> None:
    connect = AsyncMock(return_value=_SdkClient())
    monkeypatch.setattr(client_module.Client, "connect", connect)

    plaintext = TemporalRuntimeConfig()
    assert isinstance(
        await IngestionTemporalClient.connect(plaintext),
        IngestionTemporalClient,
    )
    assert connect.await_args.kwargs["tls"] is None

    secure = TemporalRuntimeConfig(
        connection=TemporalConnectionConfig(
            target="temporal.example:7233",
            tls=TemporalTLSConfig(
                enabled=True,
                domain="temporal.example",
                server_root_ca_cert=b"ca",
                client_cert=b"cert",
                client_private_key=b"key",
            ),
        )
    )
    await IngestionTemporalClient.connect(secure)
    tls = connect.await_args.kwargs["tls"]
    assert tls.domain == "temporal.example"
    assert tls.client_cert == b"cert"


@pytest.mark.asyncio
async def test_client_submits_source_and_reindex_with_bounded_options() -> None:
    sdk = _SdkClient()
    config = TemporalRuntimeConfig(
        workflow_execution_timeout_seconds=123,
        workflow_task_timeout_seconds=7,
    )
    client = IngestionTemporalClient(sdk, config)

    assert await client.start(_source()) is sdk.source_handle
    source_call = sdk.start_workflow.await_args_list[0]
    assert source_call.args[0] == "harborrag.source_ingestion"
    assert source_call.kwargs["id"] == "harborrag-source:task-1"
    assert source_call.kwargs["execution_timeout"].total_seconds() == 123

    reference = await client.start_ingestion(_source())
    assert reference.run_id == "task-1"
    assert reference.workflow_id == "harborrag-source:task-1"
    assert reference.first_execution_run_id == "execution-1"

    assert await client.start_reindex(_reindex()) is sdk.reindex_handle
    reindex_call = sdk.start_workflow.await_args_list[2]
    assert reindex_call.args[0] == "harborrag.reindex"
    assert reindex_call.kwargs["id"] == "harborrag-reindex:reindex-1"
    assert reindex_call.kwargs["task_timeout"].total_seconds() == 7


@pytest.mark.asyncio
async def test_client_reads_results_progress_and_execution_status() -> None:
    sdk = _SdkClient()
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    assert (await client.result("task-1")).published == 1
    assert await client.progress("task-1") == {"published": 1}
    assert (await client.get_status("task-1")).status == "RUNNING"
    assert await client.execution_status("task-1") == "completed"
    sdk.source_handle.describe.return_value = SimpleNamespace(status=None)
    assert await client.execution_status("task-1") == "unknown"
    assert (await client.reindex_result("reindex-1")).status == "COMPLETED"


@pytest.mark.asyncio
async def test_client_health_and_controls_target_source_workflow() -> None:
    sdk = _SdkClient()
    client = IngestionTemporalClient(sdk, TemporalRuntimeConfig())

    assert await client.health() is True
    await client.pause("task-1")
    await client.resume("task-1")
    await client.cancel("task-1")

    assert [call.args[0] for call in sdk.source_handle.signal.await_args_list] == [
        "pause",
        "resume",
        "request_graceful_cancel",
    ]
    sdk.source_handle.cancel.assert_not_awaited()
