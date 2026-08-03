"""Optional graph observation attached to an authoritative retrieval response."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import DocumentIdentityBuilder, KnowledgeNodeKind
from harborrag_core.storage import StorageOperationContext

from .contracts import KnowledgeGraphReader
from .validation import required_text

logger = logging.getLogger("harborrag.runtime.retrieval")

_GRAPH_SEED_LIMIT = 3
_GRAPH_OBSERVE_DEPTH = 2
_GRAPH_OBSERVE_MAX_NODES = 100
_GRAPH_OBSERVE_DIRECTION = "both"


@dataclass(frozen=True, slots=True)
class GraphObservation:
    """Counts summarising the graph neighbourhood around the retrieved candidates."""

    nodes: int = 0
    relations: int = 0
    truncated: bool = False


class GraphObserver:
    """Summarise the graph around retrieved candidates without failing retrieval."""

    def __init__(
        self,
        graph: KnowledgeGraphReader,
        identities: DocumentIdentityBuilder,
    ) -> None:
        self._graph = graph
        self._identities = identities

    async def observe(
        self,
        candidates: Sequence[VectorSearchResult],
        *,
        context: StorageOperationContext,
        request_id: str,
    ) -> GraphObservation:
        """Return neighbourhood counts, or empty counts if observation fails.

        Graph observation is a diagnostic extra: a graph outage must degrade the
        response rather than fail a retrieval the vector store already answered.
        """

        try:
            return await self._observe(candidates, context=context)
        except Exception:
            logger.warning(
                "Optional graph observation failed",
                extra={"request_id": request_id},
                exc_info=True,
            )
            return GraphObservation()

    async def _observe(
        self,
        candidates: Sequence[VectorSearchResult],
        *,
        context: StorageOperationContext,
    ) -> GraphObservation:
        seeds = tuple(
            dict.fromkeys(
                (
                    required_text(candidate.payload, "document_id"),
                    required_text(candidate.payload, "document_version_id"),
                )
                for candidate in candidates
            )
        )[:_GRAPH_SEED_LIMIT]
        traversals = await asyncio.gather(
            *(
                self._graph.traverse(
                    self._identities.node_key(
                        node_kind=KnowledgeNodeKind.DOCUMENT,
                        logical_id=document_id,
                        document_version_id=document_version_id,
                    ),
                    max_depth=_GRAPH_OBSERVE_DEPTH,
                    max_nodes=_GRAPH_OBSERVE_MAX_NODES,
                    direction=_GRAPH_OBSERVE_DIRECTION,
                    context=context,
                )
                for document_id, document_version_id in seeds
            )
        )
        node_keys = {node.node_key for traversal in traversals for node in traversal.nodes}
        relation_ids = {
            relation.relation_id for traversal in traversals for relation in traversal.relations
        }
        return GraphObservation(
            nodes=len(node_keys),
            relations=len(relation_ids),
            truncated=any(traversal.truncated for traversal in traversals),
        )


__all__ = ["GraphObservation", "GraphObserver"]
