from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from harborrag_core.observability.process_logging import configure_logging
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.ingestion import build_ingestion_runtime

from .connection import connect_temporal_client
from .ingestion_activities import IngestionActivities
from .maintenance_activities import MaintenanceActivities
from .worker_registry import (
    validate_worker_registrations,
    worker_registrations,
)

logger = logging.getLogger("harborrag.runtime.temporal.worker")
TASK_QUEUE_DEPTH_LOOKUP_TIMEOUT_SECONDS = 1.0


async def run_workers(
    settings: RuntimeSettings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the Postgres-authoritative workflow hierarchy until shutdown."""

    config = TemporalRuntimeConfig.from_settings(settings)
    # Establish the control-plane dependency before opening every ingestion
    # repository and connector. This fails fast during Temporal startup or
    # transport errors and avoids expensive start/close churn in restart loops.
    client = await connect_temporal_client(config)
    runtime = build_ingestion_runtime(settings)
    try:
        await runtime.start()
        activities = IngestionActivities(
            runtime,
            telemetry=runtime.telemetry,
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
        logger.info(
            "Temporal ingestion worker polling queues: %s",
            ", ".join(config.task_queues.as_tuple()),
        )
        runs = asyncio.gather(*(worker.run() for worker in workers))
        await _wait_for_shutdown(workers, runs, stop_event=stop_event)
    finally:
        await runtime.close()


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
        identity=worker.identity,
        max_concurrent_activities=worker.max_concurrent_activities,
        max_concurrent_workflow_tasks=worker.max_concurrent_workflow_tasks,
        max_concurrent_activity_task_polls=(worker.max_concurrent_activity_polls),
        max_concurrent_workflow_task_polls=(worker.max_concurrent_workflow_polls),
        graceful_shutdown_timeout=timedelta(seconds=worker.graceful_shutdown_seconds),
    )


async def _emit_queue_metrics(
    telemetry: Any,
    client: Client,
    config: TemporalRuntimeConfig,
) -> None:
    # Runtime-owned telemetry is optional in tests; skip emission when unavailable.
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


async def _describe_task_queue_depths(
    client: Client,
    *,
    namespace: str,
    task_queues: tuple[str, ...],
) -> dict[str, int]:
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
        except BaseException as exc:  # Preserve cancellation after every worker was stopped.
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
    previous: dict[signal.Signals, Any] = {}
    for process_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[process_signal] = signal.getsignal(process_signal)
            loop.add_signal_handler(process_signal, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            previous.pop(process_signal, None)
    return previous


def main() -> None:
    configure_logging()
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
