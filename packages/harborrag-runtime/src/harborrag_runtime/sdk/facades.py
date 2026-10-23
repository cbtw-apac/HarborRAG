"""SDK ingestion façade and compatibility exports for reader façades."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_runtime.contracts import (
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
    IngestionTaskReference,
)
from harborrag_runtime.retrieval.facades import (
    GraphFacade,
    KnowledgeFacade,
    RetrievalFacade,
)

if TYPE_CHECKING:
    from .runtime import HarborRAG


class IngestionFacade:
    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def run(self, request: IngestionRequest) -> IngestionResult:
        return await self._owner._ingestion_run(request)

    async def submit(self, request: IngestionRequest) -> IngestionTaskReference:
        return await self._owner._ingestion_submit(request)

    async def status(self, task_id: str) -> IngestionStatus:
        return await self._owner._ingestion_status(task_id)

    async def pause(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "pause")

    async def resume(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "resume")

    async def cancel(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "cancel")


__all__ = ["GraphFacade", "IngestionFacade", "KnowledgeFacade", "RetrievalFacade"]
