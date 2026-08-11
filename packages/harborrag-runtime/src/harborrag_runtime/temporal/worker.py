from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from harborrag_core.observability.process_logging import configure_logging
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.ingestion import build_ingestion_runtime

from .document_workflow import DocumentIngestionWorkflow
from .ingestion_activities import IngestionActivities
from .maintenance_activities import (
    MaintenanceActivities,
)
from .policies import (
    DISCOVERY_QUEUE,
    INDEX_QUEUE,
    IO_QUEUE,
    MODEL_QUEUE,
    PARSER_QUEUE,
    TRANSFORM_QUEUE,
)
from .reindex_workflow import ReindexWorkflow
from .retry_workflow import DocumentRetryWorkflow, RetryFailuresWorkflow
from .source_workflow import (
    SourceBatchWorkflow,
    SourceIngestionWorkflow,
)

logger = logging.getLogger("harborrag.runtime.temporal.worker")

ALL_TASK_QUEUES = (
    DISCOVERY_QUEUE,
    TRANSFORM_QUEUE,
    IO_QUEUE,
    PARSER_QUEUE,
    MODEL_QUEUE,
    INDEX_QUEUE,
)
EXPECTED_WORKFLOWS = {
    "SourceIngestionWorkflow",
    "RetryFailuresWorkflow",
    "SourceBatchWorkflow",
    "DocumentIngestionWorkflow",
    "DocumentRetryWorkflow",
    "ReindexWorkflow",
}
EXPECTED_ACTIVITIES = {
    "discover_source_items",
    "cancel_source_ingestion",
    "record_source_failure",
    "finalize_source_ingestion",
    "prepare_retry_failures",
    "record_retry_failures_task_failure",
    "finalize_retry_failures",
    "sync_content_units",
    "chunk_and_validate",
    "build_relations",
    "build_projections",
    "fetch_and_capture_raw",
    "persist_canonical",
    "record_document_failure",
    "retry_document_release",
    "record_retry_document_failure",
    "parse_and_normalize",
    "encode_chunks",
    "write_vector_projection",
    "write_graph_projection",
    "verify_projections",
    "publish_version",
    "cleanup_source_projections",
    "cleanup_reindex_projections",
    "repair_reindex_relations",
    "reindex",
}
TASK_QUEUE_DEPTH_LOOKUP_TIMEOUT_SECONDS = 1.0


async def run_workers(
    settings: RuntimeSettings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the Postgres-authoritative workflow hierarchy until shutdown."""

    config = TemporalRuntimeConfig.from_settings(settings)
    runtime = build_ingestion_runtime(settings)
    await runtime.start()
    try:
        client = await _connect_client(config)
        activities = IngestionActivities(
            runtime,
            telemetry=runtime.telemetry,
        )
        maintenance = MaintenanceActivities(
            runtime,
            telemetry=runtime.telemetry,
        )
        registrations = _worker_registrations(activities, maintenance)
        _validate_worker_registrations(registrations)
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
            ", ".join(ALL_TASK_QUEUES),
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


def _worker_registrations(
    activities: IngestionActivities,
    maintenance: MaintenanceActivities,
) -> tuple[
    tuple[str, tuple[type[Any], ...], tuple[Callable[..., Any], ...]],
    ...,
]:
    return (
        (
            DISCOVERY_QUEUE,
            (SourceIngestionWorkflow, RetryFailuresWorkflow),
            (
                activities.discover_source_items,
                activities.cancel_source_ingestion,
                activities.record_source_failure,
                activities.finalize_source_ingestion,
                activities.prepare_retry_failures,
                activities.record_retry_failures_task_failure,
                activities.finalize_retry_failures,
            ),
        ),
        (
            TRANSFORM_QUEUE,
            (
                SourceBatchWorkflow,
                DocumentIngestionWorkflow,
                DocumentRetryWorkflow,
            ),
            (
                activities.sync_content_units,
                activities.chunk_and_validate,
                activities.build_relations,
                activities.build_projections,
            ),
        ),
        (
            IO_QUEUE,
            (),
            (
                activities.fetch_and_capture_raw,
                activities.persist_canonical,
                activities.record_document_failure,
                activities.retry_document_release,
                activities.record_retry_document_failure,
            ),
        ),
        (
            PARSER_QUEUE,
            (),
            (activities.parse_and_normalize,),
        ),
        (
            MODEL_QUEUE,
            (),
            (activities.encode_chunks,),
        ),
        (
            INDEX_QUEUE,
            (ReindexWorkflow,),
            (
                activities.write_vector_projection,
                activities.write_graph_projection,
                activities.verify_projections,
                activities.publish_version,
                maintenance.cleanup_source_projections,
                maintenance.cleanup_reindex_projections,
                maintenance.repair_reindex_relations,
                maintenance.reindex,
            ),
        ),
    )


def _validate_worker_registrations(
    registrations: tuple[
        tuple[str, tuple[type[Any], ...], tuple[Callable[..., Any], ...]],
        ...,
    ],
) -> None:
    queue_names = tuple(task_queue for task_queue, _, _ in registrations)
    if set(queue_names) != set(ALL_TASK_QUEUES):
        missing = sorted(set(ALL_TASK_QUEUES) - set(queue_names))
        unexpected = sorted(set(queue_names) - set(ALL_TASK_QUEUES))
        raise RuntimeError(
            f"Temporal worker queue registration mismatch missing={missing} unexpected={unexpected}"
        )

    workflow_names = [
        workflow_type.__name__ for _, workflows, _ in registrations for workflow_type in workflows
    ]
    activity_names = [
        activity_fn.__name__ for _, _, activities in registrations for activity_fn in activities
    ]

    duplicate_workflows = sorted(
        {name for name in workflow_names if workflow_names.count(name) > 1}
    )
    duplicate_activities = sorted(
        {name for name in activity_names if activity_names.count(name) > 1}
    )
    missing_workflows = sorted(EXPECTED_WORKFLOWS - set(workflow_names))
    missing_activities = sorted(EXPECTED_ACTIVITIES - set(activity_names))
    unexpected_workflows = sorted(set(workflow_names) - EXPECTED_WORKFLOWS)
    unexpected_activities = sorted(set(activity_names) - EXPECTED_ACTIVITIES)
    if any(
        (
            duplicate_workflows,
            duplicate_activities,
            missing_workflows,
            missing_activities,
            unexpected_workflows,
            unexpected_activities,
        )
    ):
        raise RuntimeError(
            "Temporal worker registrations are incomplete or inconsistent "
            f"duplicate_workflows={duplicate_workflows} "
            f"duplicate_activities={duplicate_activities} "
            f"missing_workflows={missing_workflows} "
            f"missing_activities={missing_activities} "
            f"unexpected_workflows={unexpected_workflows} "
            f"unexpected_activities={unexpected_activities}"
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
        task_queues=ALL_TASK_QUEUES,
    )
    for queue_name in ALL_TASK_QUEUES:
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


async def _connect_client(config: TemporalRuntimeConfig) -> Client:
    connection = config.connection
    tls: bool | TLSConfig | None = None
    if connection.tls.enabled:
        tls = TLSConfig(
            server_root_ca_cert=connection.tls.server_root_ca_cert,
            domain=connection.tls.domain,
            client_cert=connection.tls.client_cert,
            client_private_key=connection.tls.client_private_key,
        )
    return await Client.connect(
        connection.target,
        namespace=connection.namespace,
        identity=connection.identity,
        api_key=connection.api_key,
        tls=tls,
    )


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
