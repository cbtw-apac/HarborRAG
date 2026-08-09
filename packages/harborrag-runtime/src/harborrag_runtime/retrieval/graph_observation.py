"""Optional graph observation attached to an authoritative retrieval response."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import GraphNodeRecord, KnowledgeNodeKind
from harborrag_core.storage import StorageOperationContext

from .contracts import GraphDocumentSummary, KnowledgeGraphReader
from .validation import required_text

logger = logging.getLogger("harborrag.runtime.retrieval")

# Seeds cover the whole result set rather than an arbitrary prefix: provenance that
# describes three of ten results is worse than none, because a caller cannot tell which
# results it covers. The cap bounds the fan-out for large top_k.
_GRAPH_SEED_LIMIT = 10
# Depth 2 is exactly what provenance needs: a chunk reaches its Structure in one hop
# (:Chunk)-[:SUPPORTS]->(:Structure) and the owning DocumentVersion in a second
# (:DocumentVersion)-[:CONTAINS]->(:Structure). Depth 4 paid for two more hops the summary
# never read. Note this is not a net saving -- raising the seed count from 3 to 10
# outweighs the shallower walk (measured ~0.6ms -> ~2.2ms of FalkorDB time on a 3.2k-node
# graph). The traversals are gathered concurrently, and observation is opt-in.
_GRAPH_OBSERVE_DEPTH = 2
_GRAPH_OBSERVE_MAX_NODES = 100
# Undirected: SUPPORTS points from the chunk into the spine while CONTAINS points down
# it, so a directed walk cannot reach a chunk's own document.
_GRAPH_OBSERVE_DIRECTION = "both"


@dataclass(frozen=True, slots=True)
class GraphObservation:
    """Structure surrounding the retrieved candidates.

    The counts answer "is the graph populated here"; ``documents`` answers "what are
    these results, structurally" -- the grouping a vector payload cannot express, since
    each payload knows only its own document and section.
    """

    nodes: int = 0
    relations: int = 0
    truncated: bool = False
    documents: tuple[GraphDocumentSummary, ...] = field(default_factory=tuple)


class GraphObserver:
    """Summarise the graph around retrieved candidates without failing retrieval."""

    def __init__(
        self,
        graph: KnowledgeGraphReader,
    ) -> None:
        self._graph = graph

    async def observe(
        self,
        candidates: Sequence[VectorSearchResult],
        *,
        context: StorageOperationContext,
        request_id: str,
    ) -> GraphObservation:
        """Return the surrounding structure, or an empty summary if observation fails.

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
            dict.fromkeys(required_text(candidate.payload, "chunk_id") for candidate in candidates)
        )[:_GRAPH_SEED_LIMIT]
        if not seeds:
            return GraphObservation()
        traversals = await asyncio.gather(
            *(
                self._graph.traverse(
                    chunk_id,
                    max_depth=_GRAPH_OBSERVE_DEPTH,
                    max_nodes=_GRAPH_OBSERVE_MAX_NODES,
                    direction=_GRAPH_OBSERVE_DIRECTION,
                    context=context,
                )
                for chunk_id in seeds
            )
        )
        nodes = {node.node_key: node for traversal in traversals for node in traversal.nodes}
        relation_ids = {
            relation.relation_id for traversal in traversals for relation in traversal.relations
        }
        return GraphObservation(
            nodes=len(nodes),
            relations=len(relation_ids),
            truncated=any(traversal.truncated for traversal in traversals),
            documents=_summarise_documents(tuple(nodes.values())),
        )


def _summarise_documents(
    nodes: Sequence[GraphNodeRecord],
) -> tuple[GraphDocumentSummary, ...]:
    """Group traversal nodes by the document they belong to.

    Titles come from the DocumentVersion node and sections from Structure nodes; chunk
    nodes carry neither, which is why the grouping needs the graph rather than the
    vector payload.
    """

    titles: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    for node in nodes:
        if node.document_id is None:
            continue
        key = str(node.document_id)
        if key not in sections:
            sections[key] = []
            order.append(key)
        if node.title is None:
            continue
        if node.node_kind == KnowledgeNodeKind.DOCUMENT_VERSION:
            titles.setdefault(key, node.title)
        elif node.node_kind == KnowledgeNodeKind.STRUCTURE and node.title not in sections[key]:
            sections[key].append(node.title)
    return tuple(
        GraphDocumentSummary(
            document_id=key,
            title=titles.get(key),
            sections=tuple(sections[key]),
        )
        for key in order
    )


__all__ = ["GraphDocumentSummary", "GraphObservation", "GraphObserver"]
