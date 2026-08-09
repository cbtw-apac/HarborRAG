"""Provider-independent repositories used by retrieval policies."""

from __future__ import annotations

from typing import Protocol

from harborrag_core.ingestion import KnowledgeGraphTraversal
from harborrag_core.retrieval import (
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext


class GraphRetrievalRepositoryPort(Protocol):
    async def search_triplets(
        self,
        query: GraphTripletQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphTripletResult: ...

    async def find_paths(
        self,
        query: GraphPathQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphPathResult: ...

    async def expand_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal: ...
