"""Tenant-scoped administration for rebuildable retrieval projections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import asdict, dataclass

from harborrag_adapters.repositories.vector import HarborVectorRepository
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.composition.resources import (
    build_knowledge_graph,
    build_vector_repository,
)
from harborrag_runtime.config.settings import RuntimeSettings

_VECTOR_INDEXES = ("evidence",)
_STORES = frozenset({"vector", "graph"})


@dataclass(frozen=True, slots=True)
class VectorCollectionInventory:
    logical_name: str
    physical_name: str
    exists: bool


@dataclass(frozen=True, slots=True)
class TenantProjectionInventory:
    tenant: str
    vector_collections: tuple[VectorCollectionInventory, ...]
    graph_name: str
    graph_nodes: int
    graph_relations: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TenantProjectionDeletion:
    tenant: str
    deleted_stores: tuple[str, ...]
    before: TenantProjectionInventory
    reindex_required: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class ProjectionAdministrationService:
    """Inspect and delete one tenant's non-authoritative retrieval projections."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        vectors: HarborVectorRepository | None = None,
        graph: KnowledgeGraphRepositoryPort | None = None,
    ) -> None:
        self._settings = settings
        self._vectors = vectors or build_vector_repository(settings)
        self._graph = graph or build_knowledge_graph(settings)
        self._started: bool = False
        self._start_lock = asyncio.Lock()

    async def inspect(self, tenant: str) -> TenantProjectionInventory:
        await self._start()
        context = self._context(tenant, "inspect_tenant_projection")
        exists, graph_counts = await asyncio.gather(
            asyncio.gather(
                *(self._vectors.index_exists(name, context=context) for name in _VECTOR_INDEXES)
            ),
            self._graph.tenant_projection_counts(context=context),
        )
        graph_nodes, graph_relations = graph_counts
        return TenantProjectionInventory(
            tenant=tenant,
            vector_collections=tuple(
                VectorCollectionInventory(
                    logical_name=name,
                    physical_name=(f"{self._settings.qdrant_collection_prefix}{tenant}_{name}"),
                    exists=present,
                )
                for name, present in zip(_VECTOR_INDEXES, exists, strict=True)
            ),
            graph_name=self._settings.falkordb_graph,
            graph_nodes=graph_nodes,
            graph_relations=graph_relations,
        )

    async def delete(
        self,
        tenant: str,
        *,
        confirmation: str,
        stores: frozenset[str] = _STORES,
    ) -> TenantProjectionDeletion:
        if confirmation != tenant:
            raise HarborValidationError(
                "projection deletion confirmation must exactly match the tenant"
            )
        unknown = stores - _STORES
        if not stores or unknown:
            raise HarborValidationError("projection stores must contain vector, graph, or both")
        before = await self.inspect(tenant)
        context = self._context(tenant, "delete_tenant_projection")
        operations: list[Awaitable[None]] = []
        if "vector" in stores:
            operations.extend(
                self._vectors.delete_index(name, context=context) for name in _VECTOR_INDEXES
            )
        if "graph" in stores:
            operations.append(self._graph.delete_tenant_projection(context=context))
        await asyncio.gather(*operations)
        return TenantProjectionDeletion(
            tenant=tenant,
            deleted_stores=tuple(sorted(stores)),
            before=before,
        )

    async def close(self) -> None:
        if not self._started:
            return
        await asyncio.gather(self._vectors.close(), self._graph.close())
        self._started = False

    async def _start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            try:
                await asyncio.gather(self._vectors.connect(), self._graph.connect())
            except BaseException:
                await asyncio.gather(
                    self._vectors.close(),
                    self._graph.close(),
                    return_exceptions=True,
                )
                raise
            self._started = True

    @staticmethod
    def _context(tenant: str, operation_kind: str) -> StorageOperationContext:
        return StorageOperationContext.system(tenant, operation_kind=operation_kind)
