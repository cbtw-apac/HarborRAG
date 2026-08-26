from __future__ import annotations

from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.contracts import (
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
    IngestionTaskReference,
)
from harborrag_runtime.execution.submission import build_ingestion_input
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.schemas import SourceIngestionInput


class TemporalIngestionExecutor:
    """Execute ingestion through durable Temporal workflows."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._client: IngestionTemporalClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = await IngestionTemporalClient.connect(
                TemporalRuntimeConfig.from_settings(self._settings)
            )

    async def _connected_client(self) -> IngestionTemporalClient:
        await self.start()
        if self._client is None:
            raise HarborInvariantError("Temporal client must be initialized after start")
        return self._client

    def _input(self, request: IngestionRequest) -> SourceIngestionInput:
        return build_ingestion_input(self._settings, request)

    async def submit(self, request: IngestionRequest) -> IngestionTaskReference:
        client = await self._connected_client()
        reference = await client.start_ingestion(self._input(request))
        return IngestionTaskReference(reference.run_id, reference.workflow_id)

    async def run(self, request: IngestionRequest) -> IngestionResult:
        await self.submit(request)
        client = await self._connected_client()
        result = await client.result(request.task_id)
        return IngestionResult(
            task_id=result.task_id,
            status=result.status,
            discovered=result.discovered,
            published=result.published,
            unchanged=result.unchanged,
            failed=result.failed,
        )

    async def status(self, task_id: str) -> IngestionStatus:
        client = await self._connected_client()
        status = await client.get_status(task_id)
        return IngestionStatus(
            task_id=status.task_id,
            status=status.status,
            paused=status.paused,
            cancel_requested=status.cancel_requested,
        )

    async def pause(self, task_id: str) -> None:
        client = await self._connected_client()
        await client.pause(task_id)

    async def resume(self, task_id: str) -> None:
        client = await self._connected_client()
        await client.resume(task_id)

    async def cancel(self, task_id: str) -> None:
        client = await self._connected_client()
        await client.cancel(task_id)

    async def aclose(self) -> None:
        self._client = None
