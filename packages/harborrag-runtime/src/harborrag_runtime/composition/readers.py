"""Reader-only service composition and ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_runtime.observability.tool_audit import build_tool_execution_audit
from harborrag_runtime.retrieval.facades import GraphFacade, KnowledgeFacade, RetrievalFacade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.config_store import CachedConfigProvider
    from harborrag_runtime.retrieval.service import RuntimeRetrievalService


@dataclass
class ReaderApplication:
    settings: RuntimeSettings
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)
    _service: RuntimeRetrievalService | None = field(default=None, init=False, repr=False)
    config: CachedConfigProvider | None = field(default=None, init=False, repr=False)
    _config_engine: AsyncEngine | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.retrieval = RetrievalFacade(self)
        self.graph = GraphFacade(self)
        self.knowledge = KnowledgeFacade(self)
        self.tools = build_reader_tool_catalog(self, self.references)
        self.invoker = ToolInvoker(self.tools, audit=build_tool_execution_audit())

    async def _retrieval_service(self) -> RuntimeRetrievalService:
        async with self._lock:
            if self._service is None:
                from harborrag_runtime.retrieval.composition import connect_retrieval_service

                self._service = await connect_retrieval_service(self.settings)
            return self._service

    async def start(self) -> None:
        await self._start_config()
        await self._retrieval_service()

    async def _start_config(self) -> None:
        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
        )
        from harborrag_runtime.composition.config_store import build_config_provider

        engine = create_control_plane_engine(self.settings.control_db_url.get_secret_value())
        provider = build_config_provider(self.settings, engine=engine)
        try:
            await provider.start()
        except BaseException:
            await engine.dispose()
            raise
        self._config_engine, self.config = engine, provider

    async def aclose(self) -> None:
        if self.config is not None and self._config_engine is not None:
            provider, engine = self.config, self._config_engine
            self.config = self._config_engine = None
            await provider.aclose()
            await engine.dispose()
        async with self._lock:
            if self._service is not None:
                service, self._service = self._service, None
                await service.aclose()


def open_reader_application(settings: RuntimeSettings) -> ReaderApplication:
    return ReaderApplication(settings)
