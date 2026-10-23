"""Summary queue dispatch is durable, bounded, and stops with its host worker."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from harborrag_runtime.topology import summary_worker
from harborrag_runtime.topology.summary_worker import (
    SummaryActivities,
    _dispatch,
    _poll_delay,
)
from harborrag_runtime.topology.summary_workflow import SummaryWorkflowInput


class _WorkerContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_server_initializes_managed_tenants_and_registers_dedicated_queue(
    monkeypatch,
) -> None:
    factory = SimpleNamespace(
        settings=SimpleNamespace(
            summary_task_queue="summaries",
            temporal_max_concurrent_activities=2,
            temporal_graceful_shutdown_seconds=30,
            topology_poll_seconds=5,
        ),
        service=Mock(return_value=object()),
        initialize=AsyncMock(),
        synchronize=AsyncMock(),
    )
    worker = Mock(return_value=_WorkerContext())
    monkeypatch.setattr(summary_worker, "Worker", worker)
    stop = asyncio.Event()
    stop.set()

    await summary_worker.serve_summaries(object(), factory, ("DEFAULT", "DEFAULT"), stop_event=stop)

    factory.initialize.assert_awaited_once_with("DEFAULT")
    factory.synchronize.assert_awaited_once_with("DEFAULT")
    assert worker.call_args.kwargs["task_queue"] == "summaries"
    assert worker.call_args.kwargs["workflows"] == [summary_worker.SummaryProjectionWorkflow]


@pytest.mark.asyncio
async def test_dispatch_synchronizes_before_starting_current_scope_workflow() -> None:
    summaries = SimpleNamespace(
        reconcile=AsyncMock(),
        runnable_scopes=AsyncMock(return_value=(("docs", 7, 3),)),
    )
    factory = SimpleNamespace(
        settings=SimpleNamespace(topology_job_seconds=300, summary_task_queue="summaries"),
        synchronize=AsyncMock(),
        control=SimpleNamespace(summaries=summaries),
    )
    client = SimpleNamespace(start_workflow=AsyncMock())

    await _dispatch(client, factory, "DEFAULT")

    factory.synchronize.assert_awaited_once_with("DEFAULT")
    summaries.reconcile.assert_awaited_once_with("DEFAULT")
    call = client.start_workflow.await_args
    assert call.kwargs["id"] == "harborrag-summary:DEFAULT:docs:7:3"
    assert call.kwargs["task_queue"] == "summaries"


@pytest.mark.asyncio
async def test_duplicate_dispatch_is_idempotent_and_stop_interrupts_poll_delay() -> None:
    summaries = SimpleNamespace(
        reconcile=AsyncMock(), runnable_scopes=AsyncMock(return_value=(("docs", 1, 1),))
    )
    factory = SimpleNamespace(
        settings=SimpleNamespace(topology_job_seconds=300, summary_task_queue="summaries"),
        synchronize=AsyncMock(),
        control=SimpleNamespace(summaries=summaries),
    )
    client = SimpleNamespace(
        start_workflow=AsyncMock(side_effect=WorkflowAlreadyStartedError("workflow", "run"))
    )
    await _dispatch(client, factory, "DEFAULT")
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(_poll_delay(60, stop), timeout=0.1)


@pytest.mark.asyncio
async def test_activity_and_single_tenant_entry_point_delegate(monkeypatch) -> None:
    service = SimpleNamespace(run_once=AsyncMock(return_value="current"))
    request = SummaryWorkflowInput("tenant", "scope", 30)
    heartbeat = Mock()
    monkeypatch.setattr(summary_worker.activity, "heartbeat", heartbeat)

    assert await SummaryActivities(service).project(request) == "current"
    callback = service.run_once.call_args.kwargs["heartbeat"]
    callback()
    heartbeat.assert_called_once_with("scope")

    serve = AsyncMock()
    monkeypatch.setattr(summary_worker, "serve_summaries", serve)
    client, factory = object(), object()
    await summary_worker.watch_summaries(client, factory, "tenant")
    serve.assert_awaited_once_with(client, factory, ("tenant",), stop_event=None)


@pytest.mark.asyncio
async def test_server_skips_empty_tenants_and_dispatches_until_stopped(monkeypatch) -> None:
    factory = SimpleNamespace(settings=SimpleNamespace())
    await summary_worker.serve_summaries(object(), factory, ())

    factory = SimpleNamespace(
        settings=SimpleNamespace(
            summary_task_queue="summaries",
            temporal_max_concurrent_activities=2,
            temporal_graceful_shutdown_seconds=30,
            topology_poll_seconds=1,
        ),
        service=Mock(return_value=object()),
        initialize=AsyncMock(),
        synchronize=AsyncMock(),
    )
    monkeypatch.setattr(summary_worker, "Worker", Mock(return_value=_WorkerContext()))
    stop = asyncio.Event()

    async def dispatch(*_args):
        stop.set()

    monkeypatch.setattr(summary_worker, "_dispatch", dispatch)
    delay = AsyncMock()
    monkeypatch.setattr(summary_worker, "_poll_delay", delay)
    await summary_worker.serve_summaries(object(), factory, ("tenant",), stop_event=stop)
    delay.assert_awaited_once_with(1, stop)


@pytest.mark.asyncio
async def test_poll_delay_supports_unbounded_and_timed_polling(monkeypatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr(summary_worker.asyncio, "sleep", sleep)
    await _poll_delay(2, None)
    sleep.assert_awaited_once_with(2)

    async def timeout(awaitable, *, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(summary_worker.asyncio, "wait_for", timeout)
    await _poll_delay(2, asyncio.Event())


@pytest.mark.asyncio
async def test_dispatch_failure_is_logged_backed_off_and_never_stops_the_host(
    monkeypatch, caplog
) -> None:
    """One failing reconcile must not end the loop that shares the ingestion gather."""
    factory = SimpleNamespace(
        settings=SimpleNamespace(
            summary_task_queue="summaries",
            temporal_max_concurrent_activities=2,
            temporal_graceful_shutdown_seconds=30,
            topology_poll_seconds=5,
        ),
        service=Mock(return_value=object()),
        initialize=AsyncMock(),
        synchronize=AsyncMock(),
    )
    monkeypatch.setattr(summary_worker, "Worker", Mock(return_value=_WorkerContext()))
    stop = asyncio.Event()
    calls: list[str] = []

    async def dispatch(_client, _factory, tenant_id):
        calls.append(tenant_id)
        if len(calls) <= 4 and tenant_id == "broken":
            raise ConnectionResetError("postgres failover")
        if len(calls) >= 6:
            stop.set()

    monkeypatch.setattr(summary_worker, "_dispatch", dispatch)
    delay = AsyncMock()
    monkeypatch.setattr(summary_worker, "_poll_delay", delay)

    await summary_worker.serve_summaries(object(), factory, ("broken", "healthy"), stop_event=stop)

    # The healthy tenant is still dispatched on every pass.
    assert calls == ["broken", "healthy"] * 3
    # Consecutive failed passes back off; a clean pass returns to the base poll.
    assert [call.args[0] for call in delay.await_args_list] == [10, 20, 5]
    failures = [r for r in caplog.records if r.message.startswith("Summary dispatch failed")]
    assert len(failures) == 2
    assert all(
        record.exc_info and record.exc_info[0] is ConnectionResetError for record in failures
    )


def test_backoff_is_capped() -> None:
    assert summary_worker._backoff(5, 0) == 5
    assert summary_worker._backoff(5, 30) == summary_worker._MAX_BACKOFF_SECONDS
    assert summary_worker._backoff(600, 3) == 600
