"""Programmatic and module entry point for Temporal worker processes."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import signal
from collections.abc import Callable
from typing import Any, cast

from harborrag_core.observability.process_logging import configure_logging
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import WorkerStartupError
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.lifecycle import RuntimeLifecycle
from harborrag_runtime.temporal.workers import WorkerGroup

logger = logging.getLogger("harborrag.runtime.temporal.worker")


async def run_configured_workers(
    config: TemporalRuntimeConfig,
    dependencies: RuntimeDependencies,
    groups: tuple[WorkerGroup, ...],
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Connect once and run selected workload groups until explicitly stopped.

    ``Worker.run()`` must not receive process-task cancellation directly: the
    Temporal SDK can otherwise cancel its own shutdown path and misclassify
    in-flight activities as failures. Keep the group tasks alive while
    shutdown is requested explicitly, then wait for them to drain.
    """

    logger.info(
        "Connecting Temporal worker to %s namespace %r as %r for groups: %s",
        config.connection.target,
        config.connection.namespace,
        config.connection.identity,
        ", ".join(group.value for group in groups),
    )
    lifecycle = await RuntimeLifecycle.open(config, dependencies)
    workers = tuple(lifecycle.worker_group(group) for group in groups)
    for group, worker in zip(groups, workers, strict=True):
        # Log the queue map at startup: a run that hangs with no error is
        # almost always an activity scheduled onto a queue nobody polls, and
        # this is the only place the resolved mapping is observable.
        logger.info(
            "Worker group %s polling task queues: %s",
            group.value,
            ", ".join(registration.task_queue for registration in worker.registrations),
        )
    run_future = asyncio.gather(*(worker.run() for worker in workers))
    logger.info("Temporal worker is running; waiting for tasks")
    stop_task = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    try:
        if stop_task is None:
            await asyncio.shield(run_future)
        else:
            done, _ = await asyncio.wait(
                (run_future, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if run_future in done:
                await run_future
            else:
                logger.info("Temporal worker shutdown requested")
    finally:
        if stop_task is not None:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        try:
            await asyncio.gather(*(worker.shutdown() for worker in workers))
        finally:
            try:
                await asyncio.shield(run_future)
            finally:
                await lifecycle.close()
                logger.info("Temporal worker stopped and dependencies closed")


async def _configured_main(stop_event: asyncio.Event | None = None) -> None:
    settings = RuntimeSettings()
    provider_path = settings.temporal_dependency_provider
    provider: Callable[[RuntimeSettings], object]
    if provider_path is None:
        from harborrag_runtime.ingestion_dependencies import (
            build_ingestion_dependencies,
        )

        provider = build_ingestion_dependencies
    else:
        provider = _load_provider(provider_path)
    dependencies = provider(settings)
    if inspect.isawaitable(dependencies):
        dependencies = await dependencies
    if not isinstance(dependencies, RuntimeDependencies):
        raise WorkerStartupError("Runtime dependency provider returned an invalid value")
    try:
        groups = tuple(
            WorkerGroup(value.strip())
            for value in settings.temporal_worker_groups.split(",")
            if value.strip()
        )
    except ValueError as exc:
        raise WorkerStartupError("Temporal worker group configuration is invalid") from exc
    if not groups:
        raise WorkerStartupError("At least one Temporal worker group must be configured")
    await run_configured_workers(
        TemporalRuntimeConfig.from_settings(settings),
        dependencies,
        groups,
        stop_event=stop_event,
    )


def _install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> dict[signal.Signals, Any]:
    """Translate process termination signals into explicit worker shutdown."""

    previous_handlers: dict[signal.Signals, Any] = {}
    for process_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[process_signal] = signal.getsignal(process_signal)
            loop.add_signal_handler(process_signal, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            previous_handlers.pop(process_signal, None)
    return previous_handlers


def _restore_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    previous_handlers: dict[signal.Signals, Any],
) -> None:
    """Restore handlers replaced for the lifetime of the worker process."""

    for process_signal, previous_handler in previous_handlers.items():
        loop.remove_signal_handler(process_signal)
        signal.signal(process_signal, previous_handler)


def _load_provider(path: str) -> Callable[[RuntimeSettings], object]:
    module_name, separator, attribute = path.partition(":")
    if not separator or not module_name or not attribute:
        raise WorkerStartupError("Dependency provider must use module:callable syntax")
    try:
        module = importlib.import_module(module_name)
        provider = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise WorkerStartupError(f"Could not load runtime dependency provider {path!r}") from exc
    if not callable(provider):
        raise WorkerStartupError(f"Runtime dependency provider {path!r} is not callable")
    return cast("Callable[[RuntimeSettings], object]", provider)


def main() -> None:
    """Configure process logging, then run workers until a signal stops them."""

    configure_logging()
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        stop_event = asyncio.Event()
        previous_handlers = _install_shutdown_signal_handlers(loop, stop_event)
        try:
            runner.run(_configured_main(stop_event))
        finally:
            _restore_signal_handlers(loop, previous_handlers)


if __name__ == "__main__":
    main()
