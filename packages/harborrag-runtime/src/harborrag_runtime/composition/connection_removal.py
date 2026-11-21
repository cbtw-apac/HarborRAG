"""Build the stores a connection removal needs, outside a worker runtime.

They are built and connected on first use and closed with the owning process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_engine.ingestion import VectorProjectionPolicy, VectorProjectionStore
from harborrag_engine.ingestion.maintenance.cleanup import ProjectionCleanupService
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
    from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
    from harborrag_core.ports.storage import VectorRepositoryPort
    from harborrag_runtime.config.settings import RuntimeSettings

    from .storage_providers import RuntimeKnowledgeGraphPort

    type _Stores = tuple[
        IngestionControlPlaneDatabase, VectorRepositoryPort, RuntimeKnowledgeGraphPort
    ]


class _Store(Protocol):
    """A store connected before use and closed after."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...


class ConnectionRemoval:
    """A removal service over stores this object builds and connects on first use."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._stores: _Stores | None = None
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
        """Build and connect the control database, vector store and graph once.

        When one store fails to connect, the stores that did connect are closed and
        the first connect error is raised, so the next call starts over.
        """
        async with self._lock:
            if self._service is not None:
                return self._service
            control = build_ingestion_control(self._settings)
            vectors = build_vector_repository(self._settings)
            graph = build_knowledge_graph(self._settings)
            await _connect_all((control, vectors, graph))
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


async def _connect_all(stores: Sequence[_Store]) -> None:
    """Connect every store; on a failure close the ones that connected and re-raise it."""
    results = await asyncio.gather(*(store.connect() for store in stores), return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if not errors:
        return
    await asyncio.gather(
        *(
            store.close()
            for store, result in zip(stores, results, strict=True)
            if not isinstance(result, BaseException)
        ),
        return_exceptions=True,
    )
    raise errors[0]
