"""Build the stores a connection removal needs, outside a worker runtime.

Handles:
- the ingestion control database, vector store and graph a removal touches
- building and connecting them on first use, closing them with the owning process
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_engine.ingestion import VectorProjectionPolicy, VectorProjectionStore
from harborrag_runtime.ingestion.maintenance.cleanup import ProjectionCleanupService
from harborrag_runtime.ingestion.maintenance.connection_removal import (
    ConnectionRemovalService,
    ConnectionRemovalSummary,
)

from .resources import (
    build_ingestion_control,
    build_knowledge_graph,
    build_vector_repository,
    embedding_dimensions,
)

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


class ConnectionRemoval:
    """A removal service over stores this object builds and connects on first use."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._stores: tuple[Any, Any, Any] | None = None
        self._service: ConnectionRemovalService | None = None
        self._lock = asyncio.Lock()

    async def remove(
        self, *, tenant_id: str, source_key: str, purge: bool = False
    ) -> ConnectionRemovalSummary:
        """Connect the stores if needed, then remove the connection's documents."""
        service = await self._start()
        return await service.remove(tenant_id=tenant_id, source_key=source_key, purge=purge)

    async def aclose(self) -> None:
        """Close the stores this object opened."""
        if self._stores is None:
            return
        await asyncio.gather(*(store.close() for store in self._stores), return_exceptions=True)
        self._stores = None
        self._service = None

    async def _start(self) -> ConnectionRemovalService:
        """Build and connect the control database, vector store and graph once."""
        async with self._lock:
            if self._service is not None:
                return self._service
            control = build_ingestion_control(self._settings)
            vectors = build_vector_repository(self._settings)
            graph = build_knowledge_graph(self._settings)
            await asyncio.gather(control.connect(), vectors.connect(), graph.connect())
            self._stores = (control, vectors, graph)
            self._service = ConnectionRemovalService(
                control=control,
                cleanup=ProjectionCleanupService(
                    control=control,
                    vector_store=VectorProjectionStore(
                        vectors, VectorProjectionPolicy(dimension=self._dimension())
                    ),
                    graph_store=graph,
                ),
                graph=graph,
            )
            return self._service

    def _dimension(self) -> int:
        """Embedding dimension the vector collections were created with."""
        if self._settings.embedding_dimensions:
            return self._settings.embedding_dimensions
        config = HarborEmbedClientConfig.from_file(self._settings.model_config_path)
        return embedding_dimensions(config, self._settings.embedding_model or config.default_model)
