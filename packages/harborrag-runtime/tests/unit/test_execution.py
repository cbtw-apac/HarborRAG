from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.security import AccessContext
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import ExecutionMode, IngestionRequest
from harborrag_runtime.errors import WorkflowRunAlreadyStartedError
from harborrag_runtime.execution import build_ingestion_executor
from harborrag_runtime.execution.direct import DirectIngestionExecutor
from harborrag_runtime.execution.temporal import TemporalIngestionExecutor
from harborrag_runtime.ingestion.connections import ConnectionNotFoundError
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission
from harborrag_runtime.source_query import ProcessingProfileInput
from harborrag_runtime.temporal.gateway import to_temporal_source


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
@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
async def test_direct_executor_rebuilds_runtime_after_failed_startup(
    monkeypatch, failure_type
) -> None:
    from harborrag_runtime.execution import direct as direct_module

    failed = SimpleNamespace(start=AsyncMock(side_effect=failure_type()), close=AsyncMock())
    healthy = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    builder = Mock(side_effect=[failed, healthy])
    monkeypatch.setattr(direct_module, "build_ingestion_runtime", builder)
    executor = DirectIngestionExecutor(RuntimeSettings())

    with pytest.raises(failure_type):
        await executor.start()
    failed.close.assert_awaited_once_with()

    await executor.start()
    await executor.start()

    assert builder.call_count == 2
    healthy.start.assert_awaited_once_with()
    await executor.aclose()
    healthy.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_direct_concurrent_startup_waits_for_runtime_readiness(monkeypatch) -> None:
    from harborrag_runtime.execution import direct as direct_module

    entered = asyncio.Event()
    release = asyncio.Event()

    async def start() -> None:
        entered.set()
        await release.wait()

    runtime = SimpleNamespace(start=AsyncMock(side_effect=start), close=AsyncMock())
    builder = Mock(return_value=runtime)
    monkeypatch.setattr(direct_module, "build_ingestion_runtime", builder)
    executor = DirectIngestionExecutor(RuntimeSettings())
    first = asyncio.create_task(executor.start())
    await entered.wait()
    second = asyncio.create_task(executor.start())
    try:
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)

    builder.assert_called_once()
    runtime.start.assert_awaited_once_with()
    await executor.aclose()


@pytest.mark.asyncio
async def test_direct_retains_runtime_when_startup_cleanup_fails(monkeypatch) -> None:
    from harborrag_runtime.execution import direct as direct_module

    failed = SimpleNamespace(
        start=AsyncMock(side_effect=OSError("startup failed")),
        close=AsyncMock(side_effect=[OSError("close failed"), None]),
    )
    healthy = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(
        direct_module, "build_ingestion_runtime", Mock(side_effect=[failed, healthy])
    )
    executor = DirectIngestionExecutor(RuntimeSettings())

    with pytest.raises(ExceptionGroup, match="startup and cleanup failed") as failure:
        await executor.start()
    assert [str(error) for error in failure.value.exceptions] == [
        "startup failed",
        "close failed",
    ]

    await executor.start()

    assert failed.close.await_count == 2
    healthy.start.assert_awaited_once_with()
    await executor.aclose()


@pytest.mark.asyncio
async def test_direct_shutdown_waits_for_startup_and_retries_failed_close(monkeypatch) -> None:
    from harborrag_runtime.execution import direct as direct_module

    entered = asyncio.Event()
    release = asyncio.Event()

    async def start() -> None:
        entered.set()
        await release.wait()

    runtime = SimpleNamespace(
        start=AsyncMock(side_effect=start),
        close=AsyncMock(side_effect=[OSError("close failed"), None]),
    )
    monkeypatch.setattr(direct_module, "build_ingestion_runtime", Mock(return_value=runtime))
    executor = DirectIngestionExecutor(RuntimeSettings())
    startup = asyncio.create_task(executor.start())
    await entered.wait()
    shutdown = asyncio.create_task(executor.aclose())
    try:
        await asyncio.sleep(0)
        runtime.close.assert_not_awaited()
    finally:
        release.set()
        await startup
        with pytest.raises(OSError, match="close failed"):
            await shutdown

    await executor.aclose()
    await executor.aclose()

    assert runtime.close.await_count == 2


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
            self.control = SimpleNamespace(tasks=SimpleNamespace(get=AsyncMock(return_value=None)))
            self.started = 0
            self.closed = 0

        async def start(self) -> None:
            self.started += 1

        connection_resolver = object()
        connection_stores = None

        async def connector(self, *, tenant_id: str, task_id: str, configuration_fingerprint: str):
            calls.append((task_id, configuration_fingerprint))
            return "connector"

        async def close(self) -> None:
            self.closed += 1

    runtime = Runtime()
    source_input = SimpleNamespace(
        tenant_id="tenant-1", task_id="task-1", configuration_fingerprint="fingerprint-1"
    )
    monkeypatch.setattr(
        direct_module,
        "build_ingestion_runtime",
        lambda _settings: runtime,
    )
    monkeypatch.setattr(
        direct_module, "resolve_request_connection", AsyncMock(return_value="connection")
    )
    monkeypatch.setattr(
        direct_module,
        "build_ingestion_input",
        lambda _settings, _request, *, connection: source_input,
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
        ("task-1", "fingerprint-1"),
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
    source_input = PreparedSourceSubmission(
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
    )

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

    stores = SimpleNamespace(connections=object(), revisions=object(), aclose=AsyncMock())
    monkeypatch.setattr(temporal_module, "IngestionTemporalClient", ClientFactory)
    monkeypatch.setattr(temporal_module, "build_connection_stores", Mock(return_value=stores))
    monkeypatch.setattr(
        temporal_module, "resolve_request_connection", AsyncMock(return_value="connection")
    )
    monkeypatch.setattr(
        temporal_module,
        "build_ingestion_input",
        lambda _settings, _request, *, connection: source_input,
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
    stores.aclose.assert_awaited_once_with()
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
        ("submit", to_temporal_source(source_input)),
        ("submit", to_temporal_source(source_input)),
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
        async def connect(cls, _config) -> Client:
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

    connection = object()

    def capture(_settings, source, *, connection: object):
        nonlocal captured
        captured = (source, connection)
        return expected

    monkeypatch.setattr(submission_module, "prepare_configured_source_submission", capture)

    result = submission_module.build_ingestion_input(
        RuntimeSettings(), _request(), connection=connection
    )

    assert result is expected
    assert captured is not None and captured[1] is connection
    captured = captured[0]
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


@pytest.mark.asyncio
async def test_direct_rejects_an_unknown_connection_before_connecting(monkeypatch) -> None:
    from harborrag_runtime.execution import direct as direct_module

    runtime = SimpleNamespace(
        start=AsyncMock(), close=AsyncMock(), connection_resolver=object(), connection_stores=None
    )
    monkeypatch.setattr(direct_module, "build_ingestion_runtime", Mock(return_value=runtime))
    monkeypatch.setattr(
        direct_module,
        "resolve_request_connection",
        AsyncMock(side_effect=ConnectionNotFoundError("connection 'x' does not exist")),
    )
    executor = DirectIngestionExecutor(RuntimeSettings())

    with pytest.raises(ConnectionNotFoundError, match="does not exist"):
        await executor.run(_request())

    runtime.start.assert_not_called()
    runtime.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_direct_resolves_connector_before_connecting(monkeypatch) -> None:
    from harborrag_runtime.execution import direct as direct_module

    runtime = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(),
        connection_resolver=object(),
        connection_stores=None,
        connector=AsyncMock(side_effect=ConnectorConfigurationError("source_path is missing")),
    )
    monkeypatch.setattr(direct_module, "build_ingestion_runtime", Mock(return_value=runtime))
    monkeypatch.setattr(direct_module, "resolve_request_connection", AsyncMock())
    monkeypatch.setattr(
        direct_module,
        "build_ingestion_input",
        Mock(
            return_value=SimpleNamespace(
                tenant_id="tenant-1", task_id="task-1", configuration_fingerprint="f"
            )
        ),
    )
    executor = DirectIngestionExecutor(RuntimeSettings())

    with pytest.raises(ConnectorConfigurationError, match="source_path"):
        await executor.run(_request())

    runtime.start.assert_not_called()
    runtime.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_direct_rejects_reused_terminal_run_id(monkeypatch) -> None:
    from harborrag_runtime.execution import direct as direct_module

    finished = SimpleNamespace(status=IngestionTaskState.COMPLETED)
    runtime = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(),
        connection_resolver=object(),
        connection_stores=None,
        connector=AsyncMock(return_value="connector"),
        control=SimpleNamespace(tasks=SimpleNamespace(get=AsyncMock(return_value=finished))),
        sources=SimpleNamespace(ingest=AsyncMock()),
    )
    monkeypatch.setattr(direct_module, "build_ingestion_runtime", Mock(return_value=runtime))
    monkeypatch.setattr(direct_module, "resolve_request_connection", AsyncMock())
    monkeypatch.setattr(
        direct_module,
        "build_ingestion_input",
        Mock(
            return_value=SimpleNamespace(
                tenant_id="tenant-1", task_id="task-1", configuration_fingerprint="f"
            )
        ),
    )
    executor = DirectIngestionExecutor(RuntimeSettings())

    with pytest.raises(WorkflowRunAlreadyStartedError, match="already used"):
        await executor.run(_request())

    runtime.control.tasks.get.assert_awaited_once_with("task-1")
    runtime.sources.ingest.assert_not_called()


@pytest.mark.asyncio
async def test_direct_mode_migrates_then_imports_on_a_fresh_database(tmp_path, monkeypatch) -> None:
    from harborrag_runtime.composition.connection_stores import build_connection_stores

    source = tmp_path / "docs"
    source.mkdir()
    connectors = tmp_path / "connectors.yaml"
    connectors.write_text(
        f"version: 1\nconnectors:\n  workspace:\n    provider: local\n"
        f"    settings:\n      source_path: {source}\n",
        encoding="utf-8",
    )
    settings = RuntimeSettings(
        control_db_url=f"sqlite+aiosqlite:///{tmp_path}/fresh.db",
        connector_config_path=connectors,
    )
    stores = build_connection_stores(settings)
    runtime = SimpleNamespace(connection_stores=stores)
    executor = DirectIngestionExecutor(settings)

    await executor._import_connectors_once(runtime)  # type: ignore[arg-type]
    await executor._import_connectors_once(runtime)  # type: ignore[arg-type]

    imported = await stores.connections.get_by_source_key(
        tenant_id="DEFAULT", source_key="workspace"
    )
    assert imported is not None and imported.source_type == "local_file"
    await stores.aclose()
