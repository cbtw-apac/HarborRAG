from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_adapters.repositories.lifecycle import RepositoryLifecycle
from harborrag_core.schemas.graph import (
    GraphEdge,
    GraphExpansionQuery,
    GraphNode,
    GraphStoreCapabilities,
    GraphSubgraph,
)
from harborrag_core.schemas.ids import EntityId, RelationshipId
from harborrag_core.schemas.storage import StorageOperationContext


class HarborGraphRepository(RepositoryLifecycle):
    """Defines portable graph persistence and read operations used by HarborRAG."""

    @property
    @abstractmethod
    def capabilities(self) -> GraphStoreCapabilities:
        """Return features supported by the selected graph backend."""

    @abstractmethod
    async def upsert_nodes(
        self,
        nodes: Sequence[GraphNode],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Insert or replace tenant-scoped graph nodes."""

    @abstractmethod
    async def upsert_edges(
        self,
        edges: Sequence[GraphEdge],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Insert or replace tenant-scoped graph relationships."""

    @abstractmethod
    async def get_nodes(
        self,
        ids: Sequence[EntityId],
        *,
        context: StorageOperationContext,
    ) -> list[GraphNode]:
        """Load graph nodes by stable HarborRAG identifiers."""

    @abstractmethod
    async def get_edges(
        self,
        ids: Sequence[RelationshipId],
        *,
        context: StorageOperationContext,
    ) -> list[GraphEdge]:
        """Load graph edges by stable HarborRAG identifiers."""

    @abstractmethod
    async def delete_nodes(
        self,
        ids: Sequence[EntityId],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Delete graph nodes and provider-managed dependent relationships."""

    @abstractmethod
    async def delete_edges(
        self,
        ids: Sequence[RelationshipId],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Delete graph relationships by stable identifiers."""

    @abstractmethod
    async def expand(
        self,
        query: GraphExpansionQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphSubgraph:
        """Return a bounded tenant-scoped graph expansion."""

    @abstractmethod
    async def advanced_query(
        self,
        statement: str,
        parameters: Mapping[str, Any],
        *,
        context: StorageOperationContext,
    ) -> list[dict[str, Any]]:
        """Execute a guarded provider-native graph query."""
