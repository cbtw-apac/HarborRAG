from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.worker import run_configured_workers
from harborrag_runtime.temporal.workers import (
    WorkerGroup,
    WorkerRegistration,
    worker_registrations,
)


class _Connector(BaseConnector):
    provider_name = "fake"

    def __init__(self) -> None:
        self.connected = 0
        self.closed = 0

    def connect(self) -> None:
        self.connected += 1

    def close(self) -> None:
        self.closed += 1

    def discover(self, query=None):
        return iter(())

    def load(self, record: SourceRecord) -> RawDocument:
        raise NotImplementedError


class _Resource:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1


class _ConnectFailure(_Connector):
    def connect(self) -> None:
        raise RuntimeError("connect failed")


class _CloseFailure(_Connector):
    def close(self) -> None:
        self.closed += 1
        raise RuntimeError("close failed")


def _dependencies() -> RuntimeDependencies:
    state = SimpleNamespace(health=AsyncMock(return_value={"ready": True}))
    return RuntimeDependencies(
        connectors={"fake": _Connector()},
        parser=SimpleNamespace(),
        normalizer=SimpleNamespace(),
        chunker=SimpleNamespace(),
        chunk_persistence=SimpleNamespace(),
        indexer=SimpleNamespace(),
        state=state,
        resources=(_Resource(),),
    )


class _RunningWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop = asyncio.Event()
        self.run_cancelled = False
        self.shutdown_calls = 0
        # Mirrors RuntimeWorkerGroup.registrations, which the entry point reads
        # to log the resolved task-queue map at startup.
        self.registrations = (WorkerRegistration(task_queue="test-queue"),)

    async def run(self) -> None:
        self.started.set()
        try:
            await self.stop.wait()
        except asyncio.CancelledError:
            self.run_cancelled = True
            raise

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.stop.set()


def test_worker_registration_maps_workloads_to_central_queues() -> None:
    config = TemporalRuntimeConfig()
    dependencies = _dependencies()

    discovery = worker_registrations(WorkerGroup.DISCOVERY, config, dependencies)
    processing = worker_registrations(WorkerGroup.PROCESSING, config, dependencies)
    indexing = worker_registrations(WorkerGroup.INDEXING, config, dependencies)
    maintenance = worker_registrations(WorkerGroup.MAINTENANCE, config, dependencies)

    assert discovery[0].task_queue == config.task_queues.discovery
    assert discovery[0].activity_names == (
        "harborrag.discover_artifacts",
        "harborrag.preflight_artifact",
    )
    assert tuple(item.task_queue for item in processing) == (
        config.task_queues.chunking,
        config.task_queues.connectors,
        config.task_queues.parsers,
        config.task_queues.ocr,
    )
    assert tuple(item.task_queue for item in indexing) == (
        config.task_queues.vector_index,
        config.task_queues.graph_index,
    )
    assert maintenance[0].activity_names[-1] == "harborrag.apply_resolution"


@pytest.mark.asyncio
async def test_dependency_lifecycle_is_shared_and_idempotent(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.dependencies.asyncio.to_thread",
        direct,
    )
    dependencies = _dependencies()
    connector = dependencies.connectors["fake"]
    resource = dependencies.resources[0]

    await asyncio.gather(dependencies.start(), dependencies.start())
    assert connector.connected == 1
    assert resource.started == 1
    assert (await dependencies.health())["ready"] is True

    await asyncio.gather(dependencies.close(), dependencies.close())
    assert connector.closed == 1
    assert resource.closed == 1


@pytest.mark.asyncio
async def test_dependency_start_rolls_back_connected_resources(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.dependencies.asyncio.to_thread",
        direct,
    )
    good = _Connector()
    resource = _Resource()
    dependencies = _dependencies()
    dependencies.connectors = {"good": good, "bad": _ConnectFailure()}
    dependencies.resources = (resource,)

    with pytest.raises(RuntimeError, match="connect failed"):
        await dependencies.start()

    assert good.closed == 1
    assert resource.closed == 1
    assert (await dependencies.health())["ready"] is False


@pytest.mark.asyncio
async def test_dependency_close_attempts_every_owned_resource(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.dependencies.asyncio.to_thread",
        direct,
    )
    failing = _CloseFailure()
    good = _Connector()
    resource = _Resource()
    dependencies = _dependencies()
    dependencies.connectors = {"failing": failing, "good": good}
    dependencies.resources = (resource,)
    await dependencies.start()

    with pytest.raises(BaseExceptionGroup, match="shutdown failed"):
        await dependencies.close()

    assert failing.closed == 1
    assert good.closed == 1
    assert resource.closed == 1
    assert (await dependencies.health())["ready"] is False


@pytest.mark.asyncio
async def test_configured_workers_stop_via_explicit_shutdown(monkeypatch) -> None:
    worker = _RunningWorker()
    lifecycle = SimpleNamespace(
        worker_group=lambda group: worker,
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.worker.RuntimeLifecycle.open",
        AsyncMock(return_value=lifecycle),
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(
        run_configured_workers(
            TemporalRuntimeConfig(),
            _dependencies(),
            (WorkerGroup.DISCOVERY,),
            stop_event=stop_event,
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)

    stop_event.set()
    await asyncio.wait_for(running, timeout=1)

    assert worker.shutdown_calls == 1
    assert worker.run_cancelled is False
    lifecycle.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_cancellation_does_not_cancel_worker_run(monkeypatch) -> None:
    worker = _RunningWorker()
    lifecycle = SimpleNamespace(
        worker_group=lambda group: worker,
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.worker.RuntimeLifecycle.open",
        AsyncMock(return_value=lifecycle),
    )
    running = asyncio.create_task(
        run_configured_workers(
            TemporalRuntimeConfig(),
            _dependencies(),
            (WorkerGroup.DISCOVERY,),
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=1)

    assert worker.shutdown_calls == 1
    assert worker.run_cancelled is False
    lifecycle.close.assert_awaited_once()
