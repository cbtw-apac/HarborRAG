"""The ingestion worker process: Temporal workers, maintenance loops and telemetry.

Only ``main`` configures process-wide state (logging, telemetry export, the Temporal
runtime). Everything else takes its settings and collaborators as arguments.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.runtime import OpenTelemetryConfig, Runtime, TelemetryConfig
from temporalio.worker import Worker

from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_core.observability.process_logging import configure_logging
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.ingestion import build_ingestion_runtime
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.maintenance.retention import RetiredVersionPurgeService
from harborrag_runtime.observability.export import (
    ENDPOINT_ENV_VAR,
    TelemetryService,
    configure_telemetry_export,
)
from harborrag_runtime.observability.model import IngestionTelemetry
from harborrag_runtime.topology.derived_models import RequestEmbedder
from harborrag_runtime.topology.summary_factory import SummaryRuntimeFactory
from harborrag_runtime.topology.summary_worker import serve_summaries

from .connection import connect_temporal_client
from .ingestion_activities import IngestionActivities
from .maintenance_activities import MaintenanceActivities
from .sandbox import workflow_sandbox_runner
from .schedules import TemporalScheduleBackend
from .worker_registry import (
    validate_worker_registrations,
    worker_registrations,
)

logger = logging.getLogger("harborrag.runtime.temporal.worker")
TASK_QUEUE_DEPTH_LOOKUP_TIMEOUT_SECONDS = 1.0
SCHEDULE_METRICS_INTERVAL_SECONDS = 30.0
FIRST_RETENTION_SWEEP_MAX_DELAY_SECONDS = 60.0
OTLP_METRICS_PATH = "/v1/metrics"


def thread_pool_size(config: TemporalRuntimeConfig) -> int:
    """Return one thread per activity this process may run at once.

    Connector loads, parsing, normalization and chunking all run through
    ``asyncio.to_thread``, and a Jira load sleeps in the rate limiter while it
    holds its thread. The default executor has ``cpu_count + 4`` threads (8 on
    a 4-vCPU host), which caps the documents a replica can really progress at
    about eight however many activity slots it has.
    """
    return config.worker.max_concurrent_activities * len(config.task_queues.as_tuple())


def _size_thread_pool(config: TemporalRuntimeConfig) -> None:
    """Size the loop's default executor to the activities this process may run at once."""
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=thread_pool_size(config), thread_name_prefix="harborrag-activity"
        )
    )


async def run_workers(
    settings: RuntimeSettings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the Postgres-authoritative workflow hierarchy until shutdown.

    Temporal is connected before any ingestion repository or connector opens, so a
    Temporal outage fails fast instead of churning through start and close in a
    restart loop.
    """
    config = TemporalRuntimeConfig.from_settings(settings)
    _size_thread_pool(config)
    client = await connect_temporal_client(config)
    runtime = build_ingestion_runtime(settings)
    try:
        await runtime.start()
        activities = IngestionActivities(
            runtime,
            temporal_client=client,
            telemetry=runtime.telemetry,
            settings=settings,
            temporal_config=config,
        )
        maintenance = MaintenanceActivities(
            runtime,
            telemetry=runtime.telemetry,
        )
        registrations = worker_registrations(activities, maintenance, config.task_queues)
        validate_worker_registrations(registrations, config.task_queues.as_tuple())
        workers = tuple(
            _build_worker(
                client,
                config,
                task_queue=task_queue,
                workflows=workflows,
                activities=queue_activities,
            )
            for task_queue, workflows, queue_activities in registrations
        )
        await _emit_queue_metrics(runtime.telemetry, client, config)
        log_event(
            logger,
            "temporal.worker_started",
            task_queue_count=len(config.task_queues.as_tuple()),
        )
        summary_run = _summary_runner(settings, runtime, client, stop_event)
        retention_run = _retention_runner(settings, runtime, stop_event)
        runs = asyncio.gather(
            *(worker.run() for worker in workers),
            *((summary_run,) if summary_run is not None else ()),
            *((retention_run,) if retention_run is not None else ()),
        )
        metrics_interval = getattr(settings, "schedule_metrics_interval_seconds", 0.0)
        schedule_metrics = (
            asyncio.create_task(
                _schedule_metrics_loop(
                    runtime.telemetry, client, config, stop_event, interval=metrics_interval
                )
            )
            if metrics_interval
            else None
        )
        try:
            await _wait_for_shutdown(workers, runs, stop_event=stop_event)
        finally:
            if schedule_metrics is not None:
                schedule_metrics.cancel()
                await asyncio.gather(schedule_metrics, return_exceptions=True)
    finally:
        await runtime.close()


def _retention_runner(
    settings: RuntimeSettings,
    runtime: IngestionRuntime,
    stop_event: asyncio.Event | None,
) -> Coroutine[Any, Any, None] | None:
    """Sweep every scope for retired versions past their TTL on a fixed interval."""
    interval_hours = getattr(settings, "retired_version_purge_interval_hours", 0.0)
    retention = getattr(runtime, "retention", None)
    if not interval_hours or retention is None:
        return None
    return _sweep_retired_versions(
        retention,
        interval_hours * 3600.0,
        stop_event,
        telemetry=getattr(runtime, "telemetry", None),
    )


async def _sweep_retired_versions(
    retention: RetiredVersionPurgeService,
    interval_seconds: float,
    stop_event: asyncio.Event | None,
    *,
    telemetry: IngestionTelemetry | None = None,
) -> None:
    """Sweep soon after start, then every interval.

    Waiting a full interval first meant a worker restarted more often than the
    interval (daily deploys, spot nodes) never swept at all. The first sweep waits
    a short random delay instead, so replicas started together do not all claim
    the same versions at once.
    """
    stop = stop_event or asyncio.Event()
    first_delay = min(interval_seconds, FIRST_RETENTION_SWEEP_MAX_DELAY_SECONDS)
    delay = random.uniform(0.0, first_delay)  # noqa: S311 - jitter, not security
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except TimeoutError:
            pass
        delay = interval_seconds
        try:
            batch = await _unless_stopped(retention.run_all(), stop)
        except _SweepStopped:
            return
        except Exception as error:  # noqa: BLE001 - a failed sweep waits for the next one
            log_failure(logger, "temporal.retention_sweep_failed", error, FailureKind.UNEXPECTED)
            continue
        log_event(
            logger,
            "temporal.retention_sweep_completed",
            eligible_count=batch.eligible,
            purged_count=batch.purged,
            skipped_count=batch.skipped,
            failed_count=batch.failed,
            deleted_objects_count=batch.deleted_objects,
            backlog_count=batch.backlog,
        )
        if telemetry is not None:
            telemetry.record_retention_purge(
                purged=batch.purged,
                skipped=batch.skipped,
                failed=batch.failed,
                deleted_objects=batch.deleted_objects,
                backlog=batch.backlog,
            )


class _SweepStopped(Exception):
    """The worker is shutting down while a sweep is running."""


async def _unless_stopped[T](sweep: Coroutine[Any, Any, T], stop: asyncio.Event) -> T:
    """Run one sweep, cancelling it when the worker stops.

    A sweep drains batches for minutes, and shutdown waits for this loop.
    Cancelling mid-purge is safe. The version stays PURGING under its lease and
    the next sweep, on any worker, resumes it.
    """
    task = asyncio.ensure_future(sweep)
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait((task, stopped), return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(stopped, task, return_exceptions=True)
    if task.cancelled():
        raise _SweepStopped
    return task.result()


def _summary_runner(
    settings: RuntimeSettings,
    runtime: IngestionRuntime,
    client: Client,
    stop_event: asyncio.Event | None,
) -> Coroutine[Any, Any, None] | None:
    """Attach the optional semantic projection queue to the deployed worker.

    Settings and runtime are duck-typed on purpose. Settings without graph-build
    configuration skip this queue rather than failing the worker over an optional
    projection. The worker hands over its connected vector repository and embed
    client, so an accepted entity card also becomes a searchable point. A partial
    runtime gets cards without search.
    """
    if not hasattr(settings, "graph_build_config_path"):
        return None
    graph_build = GraphBuildConfig.from_settings(settings)
    if not graph_build.summarization.enabled:
        return None
    tenants = tuple(tenant.tenant_id for tenant in graph_build.tenants)
    if not tenants:
        log_event(
            logger,
            "temporal.summary_worker_skipped",
            level=logging.WARNING,
            hint="List managed tenants in the graph-build configuration or turn summarization off.",
            reason="no_managed_tenants",
            config_path=str(settings.graph_build_config_path),
        )
        return None
    effective = graph_build.effective_settings(settings)
    vectors = getattr(runtime, "vector_repository", None)
    embed_client = getattr(runtime, "embed_client", None)
    factory = SummaryRuntimeFactory(
        effective,
        runtime.control,
        ImmutableArtifactReader(runtime.object_store),
        ImmutableArtifactWriter(runtime.object_store),
        vectors,
        RequestEmbedder(embed_client) if embed_client is not None else None,
    )
    log_event(logger, "temporal.summary_worker_started", tenant_count=len(tenants))
    return serve_summaries(client, factory, tenants, stop_event=stop_event)


def _build_worker(
    client: Client,
    config: TemporalRuntimeConfig,
    *,
    task_queue: str,
    workflows: Sequence[type[Any]] = (),
    activities: Sequence[Callable[..., Any]] = (),
) -> Worker:
    """Apply one typed capacity policy to every task-queue worker."""
    worker = config.worker
    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
        workflow_runner=workflow_sandbox_runner(workflows),
        identity=worker.identity,
        max_concurrent_activities=worker.max_concurrent_activities,
        max_concurrent_workflow_tasks=worker.max_concurrent_workflow_tasks,
        max_concurrent_activity_task_polls=(worker.max_concurrent_activity_polls),
        max_concurrent_workflow_task_polls=(worker.max_concurrent_workflow_polls),
        graceful_shutdown_timeout=timedelta(seconds=worker.graceful_shutdown_seconds),
    )


async def _emit_queue_metrics(
    telemetry: IngestionTelemetry | None,
    client: Client,
    config: TemporalRuntimeConfig,
) -> None:
    """Set each task queue's depth, slots and saturation gauges, when telemetry exists."""
    if telemetry is None:
        return

    queue_depths = await _describe_task_queue_depths(
        client,
        namespace=config.connection.namespace,
        task_queues=config.task_queues.as_tuple(),
    )
    for queue_name in config.task_queues.as_tuple():
        slots = config.worker.max_concurrent_activities
        telemetry.record_temporal_worker_slots(queue_name, slots)
        depth = queue_depths.get(queue_name)
        telemetry.record_temporal_queue_depth(queue_name, depth)
        telemetry.record_temporal_worker_slot_saturation(queue_name, slots=slots, depth=depth)


async def _schedule_metrics_loop(
    telemetry: IngestionTelemetry,
    client: Client,
    config: TemporalRuntimeConfig,
    stop_event: asyncio.Event | None,
    *,
    interval: float = SCHEDULE_METRICS_INTERVAL_SECONDS,
) -> None:
    """Record every schedule's pause state each interval until the worker stops.

    A failed round is logged and waits for the next.
    """
    while True:
        try:
            views = await TemporalScheduleBackend(client, config).list_all()
            telemetry.record_schedule_states(
                {
                    view.definition.schedule_id: (
                        view.definition.paused,
                        view.desired_paused,
                        view.num_actions_skipped_overlap,
                    )
                    for view in views
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the metrics loop must outlive one failure
            log_failure(logger, "temporal.schedule_metrics_failed", error, FailureKind.UNEXPECTED)
        if stop_event is None:
            await asyncio.sleep(interval)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue
            return


async def _describe_task_queue_depths(
    client: Client,
    *,
    namespace: str,
    task_queues: tuple[str, ...],
) -> dict[str, int]:
    """Return each task queue's approximate backlog, leaving out queues Temporal cannot describe."""
    service_client = getattr(client, "service_client", None)
    workflow_service = getattr(service_client, "workflow_service", None)
    if workflow_service is None:
        return {}

    try:
        from temporalio.api.taskqueue.v1 import TaskQueue
        from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
    except Exception:
        return {}

    depths: dict[str, int] = {}
    for queue_name in task_queues:
        try:
            response = await asyncio.wait_for(
                workflow_service.describe_task_queue(
                    DescribeTaskQueueRequest(
                        namespace=namespace,
                        task_queue=TaskQueue(name=queue_name),
                    )
                ),
                timeout=TASK_QUEUE_DEPTH_LOOKUP_TIMEOUT_SECONDS,
            )
        except Exception:
            continue
        status = getattr(response, "task_queue_status", None)
        depth: int | None = None
        for field_name in ("approximate_backlog_count", "backlog_count_hint"):
            value = getattr(status, field_name, None) if status is not None else None
            if isinstance(value, int):
                depth = max(0, value)
                break
        if depth is not None:
            depths[queue_name] = depth
    return depths


async def _wait_for_shutdown(
    workers: tuple[Worker, ...],
    runs: asyncio.Future[Any],
    *,
    stop_event: asyncio.Event | None,
) -> None:
    """Wait for the workers to end or the stop event, then shut every worker down.

    Shutdown errors and the run's own error, cancellation included, are raised together
    once every worker has stopped.
    """
    stop_task = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    try:
        if stop_task is None:
            await asyncio.shield(runs)
        else:
            done, _ = await asyncio.wait(
                (runs, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if runs in done:
                await runs
    finally:
        if stop_task is not None:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        shutdown_results = await asyncio.gather(
            *(worker.shutdown() for worker in workers),
            return_exceptions=True,
        )
        run_error: BaseException | None = None
        try:
            await asyncio.shield(runs)
        except BaseException as exc:
            run_error = exc
        shutdown_errors = [
            result for result in shutdown_results if isinstance(result, BaseException)
        ]
        if run_error is not None:
            shutdown_errors.append(run_error)
        if shutdown_errors:
            raise BaseExceptionGroup("Temporal worker shutdown failed", shutdown_errors)


def _install_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> dict[signal.Signals, Any]:
    """Set the stop event on SIGINT and SIGTERM and return the handlers they replace."""
    previous: dict[signal.Signals, Any] = {}
    for process_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[process_signal] = signal.getsignal(process_signal)
            loop.add_signal_handler(process_signal, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            previous.pop(process_signal, None)
    return previous


def export_sdk_metrics() -> None:
    """Send the Temporal SDK's own metrics over OTLP/HTTP when export is configured.

    Call before the first client connects. The SDK then reports task latencies,
    failures and slot usage next to HarborRAG's metrics. The SDK's own
    ``service_name`` label is left off, because the collector already labels
    every series with the sending service.
    """
    endpoint = os.environ.get(ENDPOINT_ENV_VAR, "").strip().rstrip("/")
    metrics = OpenTelemetryConfig(
        url=f"{endpoint}{OTLP_METRICS_PATH}", http=True, durations_as_seconds=True
    )
    Runtime.set_default(
        Runtime(telemetry=TelemetryConfig(metrics=metrics, attach_service_name=False)),
        error_if_already_set=False,
    )


def main() -> None:
    """Run the ingestion worker until SIGINT or SIGTERM."""
    configure_logging()
    if configure_telemetry_export(TelemetryService.WORKER):
        export_sdk_metrics()
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        stop_event = asyncio.Event()
        previous = _install_handlers(loop, stop_event)
        try:
            runner.run(
                run_workers(
                    RuntimeSettings(),
                    stop_event=stop_event,
                )
            )
        finally:
            for process_signal, handler in previous.items():
                loop.remove_signal_handler(process_signal)
                signal.signal(process_signal, handler)


if __name__ == "__main__":
    main()
