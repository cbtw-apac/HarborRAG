from __future__ import annotations

import asyncio
import contextlib

from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import IngestionRequest, IngestionResult
from harborrag_runtime.errors import WorkflowRunAlreadyStartedError
from harborrag_runtime.execution.source_conversion import to_source_request
from harborrag_runtime.execution.submission import build_ingestion_input
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.runtime_builder import build_ingestion_runtime

_TERMINAL_TASK_STATES = frozenset(
    {
        IngestionTaskState.COMPLETED,
        IngestionTaskState.PARTIAL,
        IngestionTaskState.FAILED,
        IngestionTaskState.CANCELLED,
    }
)


class DirectIngestionExecutor:
    """Run source ingestion inline through the production use-case graph."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._runtime: IngestionRuntime | None = None
        self._runtime_started = False
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._runtime_started:
                return
            await self._rebuild_runtime()
            await self._connect_runtime()

    async def run(self, request: IngestionRequest) -> IngestionResult:
        # Everything that can be rejected from configuration alone happens
        # before the first connection, so a bad request never waits on I/O.
        source = build_ingestion_input(self._settings, request)
        async with self._lifecycle_lock:
            fresh = not self._runtime_started
            if fresh:
                await self._rebuild_runtime()
            runtime = self._runtime
            if runtime is None:
                raise HarborInvariantError("self._runtime must not be None here")
            try:
                connector = runtime.connector(
                    request.connector_name,
                    configuration_fingerprint=source.configuration_fingerprint,
                )
            except Exception:
                if fresh:
                    # Best effort: the configuration error is the one to report.
                    with contextlib.suppress(Exception):
                        await self._close_runtime()
                raise
            if fresh:
                await self._connect_runtime()
        # A finished task cannot run again; without this the reused ID would surface
        # as an invalid state transition from deep inside the run.
        existing = await runtime.control.tasks.get(source.task_id)
        if existing is not None and existing.status in _TERMINAL_TASK_STATES:
            raise WorkflowRunAlreadyStartedError(
                f"run id {source.task_id!r} already used ({existing.status.value})"
            )
        source_request = to_source_request(source)
        outcome = await runtime.sources.ingest(source_request, connector)
        return IngestionResult(
            task_id=outcome.task_id,
            status=outcome.status.value,
            discovered=outcome.discovered,
            published=outcome.published,
            unchanged=outcome.unchanged,
            failed=outcome.failed,
        )

    async def _rebuild_runtime(self) -> None:
        await self._close_runtime()
        self._runtime = build_ingestion_runtime(self._settings)

    async def _connect_runtime(self) -> None:
        if self._runtime is None:
            raise HarborInvariantError("self._runtime must not be None here")
        try:
            await self._runtime.start()
        except BaseException as startup_error:
            try:
                await self._close_runtime()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "direct ingestion startup and cleanup failed",
                    [startup_error, cleanup_error],
                ) from None
            raise
        self._runtime_started = True

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            await self._close_runtime()

    async def _close_runtime(self) -> None:
        self._runtime_started = False
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None
