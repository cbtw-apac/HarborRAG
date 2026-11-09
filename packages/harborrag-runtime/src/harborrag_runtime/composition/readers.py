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

_POSTGRES_DSN_PREFIX = "postgresql"
_CONFIG_POOL_SIZE = 2
_CONFIG_MAX_OVERFLOW = 2


@dataclass
class ReaderApplication:
    settings: RuntimeSettings
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)
    _service: RuntimeRetrievalService | None = field(default=None, init=False, repr=False)
    config_provider: CachedConfigProvider | None = field(default=None, init=False, repr=False)
    _config_engine: AsyncEngine | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.retrieval = RetrievalFacade(self)
        self.graph = GraphFacade(self)
        self.knowledge = KnowledgeFacade(self)
        self.tools = build_reader_tool_catalog(self, self.references)
        self.invoker = ToolInvoker(self.tools, audit=build_tool_execution_audit())
        self._build_config_provider()

    async def _retrieval_service(self) -> RuntimeRetrievalService:
        async with self._lock:
            if self._service is None:
                from harborrag_runtime.retrieval.composition import connect_retrieval_service

                self._service = await connect_retrieval_service(self.settings)
            return self._service

    async def start(self) -> None:
        """Start the settings provider, then connect the retrieval backends.

        Raises:
            HarborUnavailableError: The settings store is unreachable.
        """
        if self.config_provider is not None:
            await self.config_provider.start()
        await self._retrieval_service()

    def _build_config_provider(self) -> None:
        """Build the unstarted settings provider over Postgres, so it exists before :meth:`start`."""
        dsn = self.settings.control_db_url.get_secret_value()
        if not dsn.startswith(_POSTGRES_DSN_PREFIX):
            return
        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
        )
        from harborrag_runtime.composition.config_store import build_config_provider

        self._config_engine = create_control_plane_engine(
            dsn, pool_size=_CONFIG_POOL_SIZE, max_overflow=_CONFIG_MAX_OVERFLOW, pool_pre_ping=True
        )
        self.config_provider = build_config_provider(self.settings, engine=self._config_engine)

    async def aclose(self) -> None:
        if self.config_provider is not None:
            await self.config_provider.aclose()
        if self._config_engine is not None:
            await self._config_engine.dispose()
        async with self._lock:
            if self._service is not None:
                service, self._service = self._service, None
                await service.aclose()


def open_reader_application(settings: RuntimeSettings) -> ReaderApplication:
    return ReaderApplication(settings)
