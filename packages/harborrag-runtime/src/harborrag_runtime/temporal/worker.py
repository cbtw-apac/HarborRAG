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
        workers = (
            _build_worker(
                client,
                config,
                task_queue=DISCOVERY_QUEUE,
                workflows=(SourceIngestionWorkflow, RetryFailuresWorkflow),
                activities=(
                    activities.discover_source_items,
                    activities.cancel_source_ingestion,
                    activities.record_source_failure,
                    activities.finalize_source_ingestion,
                    activities.prepare_retry_failures,
                    activities.record_retry_failures_task_failure,
                    activities.finalize_retry_failures,
                ),
            ),
            _build_worker(
                client,
                config,
                task_queue=TRANSFORM_QUEUE,
                workflows=(
                    SourceBatchWorkflow,
                    DocumentIngestionWorkflow,
                    DocumentRetryWorkflow,
                ),
                activities=(
                    activities.sync_content_units,
                    activities.chunk_and_validate,
                    activities.build_relations,
                    activities.build_projections,
                ),
            ),
            _build_worker(
                client,
                config,
                task_queue=IO_QUEUE,
                activities=(
                    activities.fetch_and_capture_raw,
                    activities.persist_canonical,
                    activities.record_document_failure,
                    activities.retry_document_release,
                    activities.record_retry_document_failure,
                ),
            ),
            _build_worker(
                client,
                config,
                task_queue=PARSER_QUEUE,
                activities=(activities.parse_and_normalize,),
            ),
            _build_worker(
                client,
                config,
                task_queue=MODEL_QUEUE,
                activities=(activities.encode_chunks,),
            ),
            _build_worker(
                client,
                config,
                task_queue=INDEX_QUEUE,
                workflows=(ReindexWorkflow,),
                activities=(
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
        logger.info(
            "Temporal ingestion worker polling queues: %s",
            ", ".join(
                (
                    DISCOVERY_QUEUE,
                    IO_QUEUE,
                    PARSER_QUEUE,
                    TRANSFORM_QUEUE,
                    MODEL_QUEUE,
                    INDEX_QUEUE,
                )
            ),
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
