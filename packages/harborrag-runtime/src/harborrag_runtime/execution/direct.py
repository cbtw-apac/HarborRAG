from __future__ import annotations

from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import IngestionRequest, IngestionResult
from harborrag_runtime.execution.submission import build_ingestion_input
from harborrag_runtime.ingestion.composition import IngestionRuntime, build_ingestion_runtime
from harborrag_runtime.temporal.conversion import to_source_request


class DirectIngestionExecutor:
    """Run source ingestion inline through the production use-case graph."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._runtime: IngestionRuntime | None = None

    async def start(self) -> None:
        if self._runtime is None:
            self._runtime = build_ingestion_runtime(self._settings)
            await self._runtime.start()

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
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None
