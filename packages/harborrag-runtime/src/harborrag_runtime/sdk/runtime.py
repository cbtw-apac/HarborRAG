"""Stable async SDK façade for HarborRAG runtime services."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

from harborrag_core.ingestion import ExecutionCapabilityError
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse, HarborChatStreamChunk
from harborrag_engine.retrieval import RetrievalLane

from ..chat import ChatFacade, ChatPrompt, RuntimeChatService
from ..contracts import (
    ExecutionMode,
    ExpandDocumentRequest,
    ExpandDocumentResponse,
    GraphPathRequest,
    GraphPathResponse,
    GraphSubgraphRequest,
    GraphSubgraphResponse,
    GraphTripletRequest,
    GraphTripletResponse,
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
    IngestionTaskReference,
    RetrievalRequest,
    RetrievalResponse,
)
from ..execution import IngestionExecutor, build_ingestion_executor
from ..execution.contracts import DurableIngestionExecutor
from .configuration import HarborRAGConfig
from .facades import GraphFacade, IngestionFacade, RetrievalFacade

if TYPE_CHECKING:
    from ..retrieval import RuntimeRetrievalService


class HarborRAG:
    """Coordinate execution and retrieval behind narrow service façades."""

    def __init__(self, config: HarborRAGConfig) -> None:
        self.config = config
        self.chat = ChatFacade(self)
        self.ingestion = IngestionFacade(self)
        self.retrieval = RetrievalFacade(self)
        self.graph = GraphFacade(self)
        self._executor: IngestionExecutor | None = None
        self._retrieval: RuntimeRetrievalService | None = None
        self._chat_runtime = RuntimeChatService(config.runtime)
        self._retrieval_lock = asyncio.Lock()

    @classmethod
    def from_config(cls, path: str | Path) -> HarborRAG:
        return cls(HarborRAGConfig.from_file(path))

    async def __aenter__(self) -> HarborRAG:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc
        await self.aclose()

    async def start(self) -> None:
        if self._executor is not None:
            return
        if self.config.discover_plugins:
            from ..plugins import discover_runtime_plugins

            discover_runtime_plugins()
        self._executor = build_ingestion_executor(
            self.config.execution_mode,
            self.config.runtime,
        )
        await self._executor.start()

    async def _ingestion_run(self, request: IngestionRequest) -> IngestionResult:
        await self.start()
        if self._executor is None:
            raise HarborInvariantError("self._executor must not be None here")
        return await self._executor.run(request)

    async def _ingestion_submit(self, request: IngestionRequest) -> IngestionTaskReference:
        executor = await self._durable_executor(
            "direct execution supports ingestion.run(), not submit()"
        )
        return await executor.submit(request)

    async def _ingestion_status(self, task_id: str) -> IngestionStatus:
        executor = await self._durable_executor("direct execution has no durable task status")
        return await executor.status(task_id)

    async def _ingestion_control(self, task_id: str, operation: str) -> None:
        executor = await self._durable_executor(
            f"direct execution cannot {operation} a durable task"
        )
        controls = {
            "pause": executor.pause,
            "resume": executor.resume,
            "cancel": executor.cancel,
        }
        await controls[operation](task_id)

    async def _durable_executor(
        self,
        capability_error: str,
    ) -> DurableIngestionExecutor:
        await self.start()
        if self.config.execution_mode is not ExecutionMode.TEMPORAL:
            raise ExecutionCapabilityError(capability_error)
        if self._executor is None:
            raise HarborInvariantError("self._executor must not be None here")
        return cast("DurableIngestionExecutor", self._executor)

    async def _retrieval_service(self) -> RuntimeRetrievalService:
        if self._retrieval is not None:
            return self._retrieval
        async with self._retrieval_lock:
            if self._retrieval is None:
                from ..retrieval.composition import connect_retrieval_service

                self._retrieval = await connect_retrieval_service(self.config.runtime)
        return self._retrieval

    async def _chat_complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> HarborChatResponse:
        return await self._chat_runtime.complete(request, prompt=prompt)

    def _chat_stream(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        return self._chat_runtime.stream(request, prompt=prompt)

    async def aclose(self) -> None:
        close_operations = []
        close_operations.append(self._chat_runtime.aclose())
        if self._retrieval is not None:
            close_operations.append(self._retrieval.aclose())
            self._retrieval = None
        if self._executor is not None:
            close_operations.append(self._executor.aclose())
            self._executor = None
        if not close_operations:
            return
        results = await asyncio.gather(*close_operations, return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        fatal = [
            result
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, Exception)
        ]
        if fatal:
            raise BaseExceptionGroup("HarborRAG resource close failed", fatal)
        if errors:
            raise ExceptionGroup("HarborRAG resource close failed", errors)


__all__ = [
    "ExecutionMode",
    "ExpandDocumentRequest",
    "ExpandDocumentResponse",
    "GraphPathRequest",
    "GraphPathResponse",
    "GraphSubgraphRequest",
    "GraphSubgraphResponse",
    "GraphTripletRequest",
    "GraphTripletResponse",
    "HarborRAG",
    "HarborRAGConfig",
    "IngestionRequest",
    "IngestionResult",
    "IngestionStatus",
    "IngestionTaskReference",
    "RetrievalLane",
    "RetrievalRequest",
    "RetrievalResponse",
]
