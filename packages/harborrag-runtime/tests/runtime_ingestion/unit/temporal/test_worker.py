"""Temporal ingestion worker composition and lifecycle."""

from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from harborrag_runtime.config.temporal import (
    TemporalConnectionConfig,
    TemporalRuntimeConfig,
    TemporalTLSConfig,
    WorkerConfig,
)
from harborrag_runtime.ingestion.observability import IngestionTelemetry
from harborrag_runtime.temporal import worker as worker_module
from harborrag_runtime.temporal import worker_registry as worker_registry_module
from harborrag_runtime.temporal.document_workflow import DocumentIngestionWorkflow
from harborrag_runtime.temporal.reindex_workflow import ReindexWorkflow
from harborrag_runtime.temporal.retry_workflow import (
    DocumentRetryWorkflow,
    RetryFailuresWorkflow,
)
from harborrag_runtime.temporal.source_workflow import (
    SourceBatchWorkflow,
    SourceIngestionWorkflow,
)


class _Worker:
    def __init__(self, finish: asyncio.Event | None = None) -> None:
        self._finish = finish
        self.shutdown = AsyncMock(side_effect=self._shutdown)

    async def _shutdown(self) -> None:
        if self._finish is not None:
            self._finish.set()

    async def run(self) -> None:
        if self._finish is not None:
            await self._finish.wait()


@pytest.mark.parametrize(
    "workflow_type",
    (
        SourceIngestionWorkflow,
        SourceBatchWorkflow,
        DocumentIngestionWorkflow,
        DocumentRetryWorkflow,
        RetryFailuresWorkflow,
        ReindexWorkflow,
    ),
)
@pytest.mark.asyncio
async def test_registered_workflows_validate_in_default_temporal_sandbox(workflow_type) -> None:
    definition = workflow._Definition.must_from_class(workflow_type)

    SandboxedWorkflowRunner().prepare_workflow(definition)


@pytest.mark.asyncio
async def test_worker_composes_all_six_task_queues(monkeypatch) -> None:
    runtime = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(),
        telemetry=IngestionTelemetry(),
        source_plans=object(),
    )
    built: list[tuple[str, tuple[object, ...], tuple[object, ...]]] = []

    def build_worker(_client, _config, *, task_queue, workflows=(), activities=()):
        built.append((task_queue, tuple(workflows), tuple(activities)))
        return _Worker()

    wait = AsyncMock()
    monkeypatch.setattr(
        worker_module.TemporalRuntimeConfig,
        "from_settings",
        lambda _settings: TemporalRuntimeConfig(),
    )
    monkeypatch.setattr(
        worker_module,
        "build_ingestion_runtime",
        lambda _settings: runtime,
    )
    monkeypatch.setattr(worker_module, "_connect_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(worker_module, "_build_worker", build_worker)
    monkeypatch.setattr(worker_module, "_wait_for_shutdown", wait)

    stop = asyncio.Event()
    await worker_module.run_workers(SimpleNamespace(), stop_event=stop)

    assert tuple(item[0] for item in built) == (
        "harborrag-discovery",
        "harborrag-transform",
        "harborrag-io",
        "harborrag-parser",
        "harborrag-model",
        "harborrag-index",
    )
    index_activity_names = {activity.__name__ for activity in built[-1][2]}
    assert "repair_reindex_relations" in index_activity_names
    runtime.start.assert_awaited_once()
    runtime.close.assert_awaited_once()
    wait.assert_awaited_once()


def test_worker_builds_sdk_worker_with_capacity_policy(monkeypatch) -> None:
    constructor = Mock(return_value=object())
    monkeypatch.setattr(worker_module, "Worker", constructor)
    config = TemporalRuntimeConfig()
    workflow = type("Workflow", (), {})

    result = worker_module._build_worker(
        object(),
        config,
        task_queue="queue-1",
        workflows=(workflow,),
        activities=(lambda: None,),
    )

    assert result is constructor.return_value
    options = constructor.call_args.kwargs
    assert options["task_queue"] == "queue-1"
    assert options["identity"] == config.worker.identity
    assert options["max_concurrent_activities"] == config.worker.max_concurrent_activities
    assert (
        options["graceful_shutdown_timeout"].total_seconds()
        == config.worker.graceful_shutdown_seconds
    )


def test_worker_registration_validation_fails_loudly_when_workflows_missing() -> None:
    registrations = (
        ("harborrag-discovery", (), ()),
        ("harborrag-transform", (), ()),
        ("harborrag-io", (), ()),
        ("harborrag-parser", (), ()),
        ("harborrag-model", (), ()),
        ("harborrag-index", (), ()),
    )

    with pytest.raises(RuntimeError, match="missing_workflows"):
        worker_registry_module.validate_worker_registrations(registrations)


@pytest.mark.asyncio
async def test_emit_queue_metrics_records_depth_and_saturation(monkeypatch) -> None:
    telemetry = Mock()
    monkeypatch.setattr(
        worker_module,
        "_describe_task_queue_depths",
        AsyncMock(return_value={"harborrag-discovery": 12, "harborrag-index": 0}),
    )
    config = TemporalRuntimeConfig(worker=WorkerConfig(max_concurrent_activities=4))

    await worker_module._emit_queue_metrics(telemetry, object(), config)

    assert telemetry.record_temporal_worker_slots.call_count == 6
    assert telemetry.record_temporal_queue_depth.call_count == 6
    assert telemetry.record_temporal_worker_slot_saturation.call_count == 6
    telemetry.record_temporal_worker_slots.assert_any_call("harborrag-discovery", 4)
    telemetry.record_temporal_queue_depth.assert_any_call("harborrag-discovery", 12)


@pytest.mark.asyncio
async def test_emit_queue_metrics_continues_when_depth_lookup_times_out(monkeypatch) -> None:
    telemetry = Mock()

    class _WorkflowService:
        async def describe_task_queue(self, request):
            queue_name = request.task_queue.name
            if queue_name == "queue-timeout":
                await asyncio.Future()
            return SimpleNamespace(task_queue_status=SimpleNamespace(approximate_backlog_count=5))

    client = SimpleNamespace(service_client=SimpleNamespace(workflow_service=_WorkflowService()))
    config = TemporalRuntimeConfig(worker=WorkerConfig(max_concurrent_activities=4))

    monkeypatch.setattr(
        worker_module,
        "TASK_QUEUE_DEPTH_LOOKUP_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        worker_module,
        "ALL_TASK_QUEUES",
        ("queue-timeout", "queue-ok"),
    )

    await worker_module._emit_queue_metrics(telemetry, client, config)

    telemetry.record_temporal_queue_depth.assert_any_call("queue-timeout", None)
    telemetry.record_temporal_queue_depth.assert_any_call("queue-ok", 5)
    telemetry.record_temporal_worker_slot_saturation.assert_any_call(
        "queue-timeout", slots=4, depth=None
    )


@pytest.mark.asyncio
async def test_worker_connects_plaintext_and_tls(monkeypatch) -> None:
    connect = AsyncMock(return_value=object())
    monkeypatch.setattr(worker_module.Client, "connect", connect)

    await worker_module._connect_client(TemporalRuntimeConfig())
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
    await worker_module._connect_client(secure)
    tls = connect.await_args.kwargs["tls"]
    assert tls.domain == "temporal.example"
    assert tls.client_private_key == b"key"


@pytest.mark.asyncio
async def test_worker_waits_for_runs_without_a_stop_event() -> None:
    workers = (_Worker(), _Worker())
    runs = asyncio.ensure_future(asyncio.sleep(0))

    await worker_module._wait_for_shutdown(
        workers,
        runs,
        stop_event=None,
    )

    assert all(worker.shutdown.await_count == 1 for worker in workers)


@pytest.mark.asyncio
async def test_worker_shutdown_unblocks_runs_after_stop() -> None:
    finish = asyncio.Event()
    workers = (_Worker(finish),)
    runs = asyncio.ensure_future(finish.wait())
    stop = asyncio.Event()
    stop.set()

    await worker_module._wait_for_shutdown(
        workers,
        runs,
        stop_event=stop,
    )

    assert workers[0].shutdown.await_count == 1
    assert runs.done()


@pytest.mark.asyncio
async def test_worker_shutdown_attempts_every_worker_before_raising() -> None:
    finish = asyncio.Event()
    healthy = _Worker(finish)
    failing = _Worker(finish)

    async def fail_after_unblocking() -> None:
        finish.set()
        raise RuntimeError("shutdown failed")

    failing.shutdown.side_effect = fail_after_unblocking
    workers = (failing, healthy)
    runs = asyncio.gather(*(worker.run() for worker in workers))
    stop = asyncio.Event()
    stop.set()

    with pytest.raises(BaseExceptionGroup):
        await worker_module._wait_for_shutdown(workers, runs, stop_event=stop)

    assert all(worker.shutdown.await_count == 1 for worker in workers)


def test_worker_installs_only_supported_signal_handlers(monkeypatch) -> None:
    installed: list[signal.Signals] = []

    class _Loop:
        def add_signal_handler(self, process_signal, _callback) -> None:
            if process_signal == signal.SIGTERM:
                raise NotImplementedError
            installed.append(process_signal)

    monkeypatch.setattr(worker_module.signal, "getsignal", lambda value: f"old-{value}")

    previous = worker_module._install_handlers(_Loop(), asyncio.Event())

    assert installed == [signal.SIGINT]
    assert set(previous) == {signal.SIGINT}
