from __future__ import annotations

import asyncio

from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import IngestionRequest, IngestionResult
from harborrag_runtime.execution.source_conversion import to_source_request
from harborrag_runtime.execution.submission import build_ingestion_input
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.runtime_builder import build_ingestion_runtime


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
            await self._close_runtime()
            self._runtime = build_ingestion_runtime(self._settings)
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

    async def run(self, request: IngestionRequest) -> IngestionResult:
        await self.start()
        if self._runtime is None:
            raise HarborInvariantError("self._runtime must not be None here")
        source = build_ingestion_input(self._settings, request)
        source_request = to_source_request(source)
        outcome = await self._runtime.sources.ingest(
            source_request,
            self._runtime.connector(
                request.connector_name,
                configuration_fingerprint=source.configuration_fingerprint,
            ),
        )
        return IngestionResult(
            task_id=outcome.task_id,
            status=outcome.status.value,
            discovered=outcome.discovered,
            published=outcome.published,
            unchanged=outcome.unchanged,
            failed=outcome.failed,
        )

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            await self._close_runtime()

    async def _close_runtime(self) -> None:
        self._runtime_started = False
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None
