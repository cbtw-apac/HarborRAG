"""Provider-independent persistence ports required by engine indexing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harborrag_core.schemas.graph import GraphEdge, GraphNode
from harborrag_core.schemas.ids import EntityId, RelationshipId
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorCollectionSpec, VectorPoint


class VectorIndexRepositoryPort(Protocol):
    """Persist and read back vector points during staged indexing."""

    async def ensure_collection(
        self,
        spec: VectorCollectionSpec,
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def upsert(
        self,
        collection: str,
        points: Sequence[VectorPoint],
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def get(
        self,
        collection: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[VectorPoint]: ...


class GraphIndexRepositoryPort(Protocol):
    """Persist and read back graph records during staged indexing."""

    async def upsert_nodes(
        self,
        nodes: Sequence[GraphNode],
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def upsert_edges(
        self,
        edges: Sequence[GraphEdge],
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def get_nodes(
        self,
        ids: Sequence[EntityId],
        *,
        context: StorageOperationContext,
    ) -> list[GraphNode]: ...

    async def get_edges(
        self,
        ids: Sequence[RelationshipId],
        *,
        context: StorageOperationContext,
    ) -> list[GraphEdge]: ...


class VectorGenerationRepositoryPort(Protocol):
    """Apply the deferred visibility changes for a validated vector plan."""

    async def activate_generation(
        self,
        collection: str,
        *,
        artifact_id: str,
        generation_id: str,
        activate_ids: Sequence[str],
        retire_ids: Sequence[str],
        delete_ids: Sequence[str],
        tombstone_ids: Sequence[str],
        context: StorageOperationContext,
    ) -> None: ...


class GraphGenerationRepositoryPort(Protocol):
    """Apply the deferred visibility changes for a validated graph plan."""

    async def activate_generation(
        self,
        *,
        artifact_id: str,
        generation_id: str,
        previous_generation_id: str | None,
        context: StorageOperationContext,
    ) -> None: ...
