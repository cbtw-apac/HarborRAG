"""Stable async SDK façade for HarborRAG runtime services."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from harborrag_core.ingestion import ExecutionCapabilityError
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse, HarborChatStreamChunk
from harborrag_engine.retrieval import RetrievalLane

from ..chat import ChatFacade, ChatPrompt, RuntimeChatService, TenantModelSources
from ..contracts import (
    ExecutionMode,
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
from ..memory import MemoryFacade
from .configuration import HarborRAGConfig
from .facades import GraphFacade, IngestionFacade, KnowledgeFacade, RetrievalFacade

if TYPE_CHECKING:
    from ..config.settings import RuntimeSettings
    from ..memory.context_service import RuntimeMemoryContextService
    from ..retrieval import RuntimeRetrievalService

type ExecutorFactory = Callable[[ExecutionMode, "RuntimeSettings"], IngestionExecutor]
type RetrievalFactory = Callable[["RuntimeSettings"], Awaitable["RuntimeRetrievalService"]]
type ChatRuntimeFactory = Callable[["RuntimeSettings"], RuntimeChatService]


async def _connect_retrieval(settings: RuntimeSettings) -> RuntimeRetrievalService:
    from ..retrieval.composition import connect_retrieval_service

    return await connect_retrieval_service(settings)


class HarborRAG:
    """Coordinate execution and retrieval behind narrow service façades."""

    def __init__(
        self,
        config: HarborRAGConfig,
        *,
        executor_factory: ExecutorFactory = build_ingestion_executor,
        retrieval_factory: RetrievalFactory = _connect_retrieval,
        chat_runtime_factory: ChatRuntimeFactory = RuntimeChatService,
    ) -> None:
        self.config = config
        self.chat = ChatFacade(self)
        self.ingestion = IngestionFacade(self)
        self.retrieval = RetrievalFacade(self)
        self.graph = GraphFacade(self)
        self.memory = MemoryFacade(self)
        self.knowledge = KnowledgeFacade(self)
        self._executor: IngestionExecutor | None = None
        self._executor_started = False
        self._plugins_discovered = False
        self._lifecycle_lock = asyncio.Lock()
        self._retrieval: RuntimeRetrievalService | None = None
        self._executor_factory = executor_factory
        self._retrieval_factory = retrieval_factory
        self._chat_runtime_factory = chat_runtime_factory
        self._chat_runtime: RuntimeChatService | None = None
        # The memory layer borrows retrieval lazily so entity anchoring can read
        # the tenant's knowledge graph without opening a graph client for
        # deployments that never resolve a mention.
        self._memory_runtime: RuntimeMemoryContextService | None = None
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
        async with self._lifecycle_lock:
            if self._executor_started:
                return
            # Retain ownership when a previous startup's cleanup failed, and
            # finish that cleanup before constructing a replacement.
            await self._close_executor()
            self._discover_plugins()
            self._executor = self._executor_factory(
                self.config.execution_mode,
                self.config.runtime,
            )
            try:
                await self._executor.start()
            except BaseException as startup_error:
                try:
                    await self._close_executor()
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "HarborRAG startup and cleanup failed",
                        [startup_error, cleanup_error],
                    ) from None
                raise
            self._executor_started = True

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
        async with self._retrieval_lock:
            if self._retrieval is None:
                self._discover_plugins()
                self._retrieval = await self._retrieval_factory(self.config.runtime)
            return self._retrieval

    def _discover_plugins(self) -> None:
        if self.config.discover_plugins and not self._plugins_discovered:
            from ..plugins import discover_runtime_plugins

            discover_runtime_plugins()
            self._plugins_discovered = True

    def configure_tenant_models(self, sources: TenantModelSources) -> None:
        """Let chat resolve each tenant's own models, once, at composition.

        Optional by construction: the SDK is built from settings alone, so a
        deployment that wires no control plane -- or has per-tenant catalogs
        switched off -- never calls this and keeps the single process-wide
        chat client it has always had.
        """

        self._chat_service().configure_tenant_models(sources)

    async def _chat_validate_model(self, model: str | None, *, tenant_id: str | None) -> None:
        await self._chat_service().validate_model(model, tenant_id=tenant_id)

    async def _chat_complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> HarborChatResponse:
        return await self._chat_service().complete(request, prompt=prompt)

    def _memory_context_service(self) -> RuntimeMemoryContextService:
        if self._memory_runtime is None:
            from ..memory.context_service import RuntimeMemoryContextService

            self._memory_runtime = RuntimeMemoryContextService(
                self.config.runtime,
                retrieval_provider=self._retrieval_service,
                index_provider=self._retrieval_service,
            )
        return self._memory_runtime

    def _chat_stream(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        return self._chat_service().stream(request, prompt=prompt)

    def _chat_service(self) -> RuntimeChatService:
        if self._chat_runtime is None:
            self._chat_runtime = self._chat_runtime_factory(self.config.runtime)
        return self._chat_runtime

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            results = await asyncio.gather(
                *([self._chat_runtime.aclose()] if self._chat_runtime is not None else []),
                *([self._memory_runtime.aclose()] if self._memory_runtime is not None else []),
                self._close_retrieval(),
                self._close_executor(),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, BaseException)]
            if failures:
                raise BaseExceptionGroup("HarborRAG resource close failed", failures)

    async def _close_retrieval(self) -> None:
        async with self._retrieval_lock:
            if self._retrieval is not None:
                await self._retrieval.aclose()
                self._retrieval = None

    async def _close_executor(self) -> None:
        self._executor_started = False
        if self._executor is not None:
            await self._executor.aclose()
            self._executor = None


__all__ = [
    "ExecutionMode",
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
