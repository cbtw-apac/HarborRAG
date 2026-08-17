from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.security import AccessContext
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import ExecutionMode, IngestionRequest
from harborrag_runtime.execution import build_ingestion_executor
from harborrag_runtime.execution.direct import DirectIngestionExecutor
from harborrag_runtime.execution.temporal import TemporalIngestionExecutor


def _request() -> IngestionRequest:
    return IngestionRequest(
        access=AccessContext(principal_id="user-1", tenant_id="tenant-1"),
        connector_name="docs",
        task_id="task-1",
        connection_id="connection-1",
        source_scope_id="scope-1",
        path="handbook",
        pattern="*.md",
        recursive=False,
        updated_after="2026-01-01T00:00:00Z",
        limit=5,
        include_attachments=False,
        filters={"team": "platform"},
        force_reprocess=True,
        discovery_page_size=25,
        discovery_concurrency=2,
        document_concurrency=3,
    )


def test_execution_factory_selects_direct_and_temporal_strategies() -> None:
    settings = RuntimeSettings()

    assert isinstance(
        build_ingestion_executor(ExecutionMode.DIRECT, settings),
        DirectIngestionExecutor,
    )
    assert isinstance(
        build_ingestion_executor(ExecutionMode.TEMPORAL, settings),
        TemporalIngestionExecutor,
    )


@pytest.mark.asyncio
async def test_direct_executor_runs_and_closes_runtime(monkeypatch) -> None:
    from harborrag_runtime.execution import direct as direct_module

    calls: list[object] = []

    class Sources:
        async def ingest(self, request, connector):
            calls.append((request, connector))
            return SimpleNamespace(
                task_id="task-1",
                status=IngestionTaskState.COMPLETED,
                discovered=4,
                published=2,
                unchanged=2,
                failed=0,
            )

    class Runtime:
        def __init__(self) -> None:
            self.sources = Sources()
            self.started = 0
            self.closed = 0

        async def start(self) -> None:
            self.started += 1

        def connector(self, name: str, *, configuration_fingerprint: str):
            calls.append((name, configuration_fingerprint))
            return "connector"

        async def close(self) -> None:
            self.closed += 1

    runtime = Runtime()
    source_input = SimpleNamespace(configuration_fingerprint="fingerprint-1")
    monkeypatch.setattr(
        direct_module,
        "build_ingestion_runtime",
        lambda _settings: runtime,
    )
    monkeypatch.setattr(
        direct_module,
        "build_ingestion_input",
        lambda _settings, _request: source_input,
    )
    monkeypatch.setattr(
        direct_module,
        "to_source_request",
        lambda source: ("converted", source),
    )
    executor = DirectIngestionExecutor(RuntimeSettings())

    await executor.start()
    result = await executor.run(_request())
    await executor.aclose()
    await executor.aclose()

    assert runtime.started == 1
    assert runtime.closed == 1
    assert calls == [
        ("docs", "fingerprint-1"),
        (("converted", source_input), "connector"),
    ]
    assert result.task_id == "task-1"
    assert result.status == "COMPLETED"
    assert (result.discovered, result.published, result.unchanged, result.failed) == (
        4,
        2,
        2,
        0,
    )


@pytest.mark.asyncio
async def test_temporal_executor_submits_controls_and_reads_results(
    monkeypatch,
) -> None:
    from harborrag_runtime.execution import temporal as temporal_module

    calls: list[object] = []
    source_input = object()

    class Client:
        async def start_ingestion(self, source):
            calls.append(("submit", source))
            return SimpleNamespace(run_id="task-1", workflow_id="workflow-1")

        async def result(self, task_id: str):
            calls.append(("result", task_id))
            return SimpleNamespace(
                task_id=task_id,
                status="completed",
                discovered=3,
                published=2,
                unchanged=1,
                failed=0,
            )

        async def get_status(self, task_id: str):
            calls.append(("status", task_id))
            return SimpleNamespace(
                task_id=task_id,
                status="running",
                paused=True,
                cancel_requested=False,
            )

        async def pause(self, task_id: str) -> None:
            calls.append(("pause", task_id))

        async def resume(self, task_id: str) -> None:
            calls.append(("resume", task_id))

        async def cancel(self, task_id: str) -> None:
            calls.append(("cancel", task_id))

    client = Client()

    class ClientFactory:
        connect_calls = 0

        @classmethod
        async def connect(cls, _config):
            cls.connect_calls += 1
            return client

    monkeypatch.setattr(temporal_module, "IngestionTemporalClient", ClientFactory)
    monkeypatch.setattr(
        temporal_module,
        "build_ingestion_input",
        lambda _settings, _request: source_input,
    )
    executor = TemporalIngestionExecutor(RuntimeSettings())
    request = _request()

    await executor.start()
    reference = await executor.submit(request)
    result = await executor.run(request)
    status = await executor.status(request.task_id)
    await executor.pause(request.task_id)
    await executor.resume(request.task_id)
    await executor.cancel(request.task_id)
    await executor.aclose()

    assert ClientFactory.connect_calls == 1
    assert (reference.task_id, reference.workflow_id) == ("task-1", "workflow-1")
    assert result.status == "completed"
    assert (result.discovered, result.published, result.unchanged, result.failed) == (
        3,
        2,
        1,
        0,
    )
    assert status.paused is True
    assert status.cancel_requested is False
    assert calls == [
        ("submit", source_input),
        ("submit", source_input),
        ("result", "task-1"),
        ("status", "task-1"),
        ("pause", "task-1"),
        ("resume", "task-1"),
        ("cancel", "task-1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "resume", "cancel"])
async def test_temporal_controls_connect_a_fresh_executor(monkeypatch, operation: str) -> None:
    from harborrag_runtime.execution import temporal as temporal_module

    calls: list[tuple[str, str]] = []

    class Client:
        async def pause(self, task_id: str) -> None:
            calls.append(("pause", task_id))

        async def resume(self, task_id: str) -> None:
            calls.append(("resume", task_id))

        async def cancel(self, task_id: str) -> None:
            calls.append(("cancel", task_id))

    class ClientFactory:
        @classmethod
        async def connect(cls, _config):
            return Client()

    monkeypatch.setattr(temporal_module, "IngestionTemporalClient", ClientFactory)
    executor = TemporalIngestionExecutor(RuntimeSettings())

    await getattr(executor, operation)("task-1")

    assert calls == [(operation, "task-1")]


def test_submission_maps_public_request_without_losing_query_controls(
    monkeypatch,
) -> None:
    from harborrag_runtime.execution import submission as submission_module

    captured = None
    expected = object()

    def capture(_settings, source):
        nonlocal captured
        captured = source
        return expected

    monkeypatch.setattr(submission_module, "build_source_input", capture)

    result = submission_module.build_ingestion_input(RuntimeSettings(), _request())

    assert result is expected
    assert captured is not None
    assert captured.task_id == "task-1"
    assert captured.tenant_id == "tenant-1"
    assert captured.connector_name == "docs"
    assert captured.connection_id == "connection-1"
    assert captured.source_scope_id == "scope-1"
    assert captured.query.path == "handbook"
    assert captured.query.pattern == "*.md"
    assert captured.query.recursive is False
    assert captured.query.updated_after == "2026-01-01T00:00:00Z"
    assert captured.query.limit == 5
    assert captured.query.include_attachments is False
    assert captured.query.filters_json == '{"team":"platform"}'
    assert captured.force_reprocess is True
    assert captured.discovery_page_size == 25
    assert captured.discovery_concurrency == 2
    assert captured.document_concurrency == 3
