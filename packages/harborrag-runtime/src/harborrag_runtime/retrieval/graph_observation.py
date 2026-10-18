"""Optional graph observation attached to an authoritative retrieval response."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import compact_node, compact_relation
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import graph_access_scope
from harborrag_engine.retrieval.graph_visibility import (
    GraphVisibilityAuthorizer,
    apply_graph_permissions,
)

from .contracts import GraphDocumentSummary, GraphResultNeighborhood, KnowledgeGraphReader
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
# Caller-supplied seeds (the graph entities this session's recalled memories
# reference) get their own allowance *on top of* the result-seed budget rather
# than sharing it. Sharing would let memory crowd out result provenance, and
# provenance that describes three of ten results is exactly what the comment
# above rejects. Five is the same order as the recall top_k that produces them.
_MEMORY_SEED_LIMIT = 5


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
        authorizer: GraphVisibilityAuthorizer | None = None,
    ) -> None:
        self._graph = graph
        self._authorizer = authorizer

    async def observe(
        self,
        candidates: Sequence[VectorSearchResult],
        *,
        context: StorageOperationContext,
        request_id: str,
        memory_seeds: Sequence[str] = (),
    ) -> GraphObservation:
        """Return the surrounding structure, or an empty summary if observation fails.

        Graph observation is a diagnostic extra: a graph outage must degrade the
        response rather than fail a retrieval the vector store already answered.

        ``memory_seeds`` widens the walk with nodes the caller already knows are
        relevant -- the entities this session's recalled memories reference --
        so the structure around them is summarised even when no vector result
        landed on them.
        """

        try:
            return await self._observe(candidates, context=context, memory_seeds=memory_seeds)
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
        memory_seeds: Sequence[str],
    ) -> GraphObservation:
        results = tuple(
            dict.fromkeys(required_text(candidate.payload, "chunk_id") for candidate in candidates)
        )[:_GRAPH_SEED_LIMIT]
        extra = tuple(
            key
            for key in dict.fromkeys(seed.strip() for seed in memory_seeds if seed.strip())
            if key not in results
        )[:_MEMORY_SEED_LIMIT]
        if not results and not extra:
            return GraphObservation()
        # Resolved once per request. A reader who can see nothing is not walked at
        # all, and an authorizer failure reaches ``observe``'s handler and degrades
        # to an empty observation -- never to an unscoped walk.
        scope = await graph_access_scope(self._authorizer, context)
        if not scope.tenant_visible:
            return GraphObservation()
        seeds = (*results, *extra)
        walks = await asyncio.gather(
            *(
                self._graph.traverse(
                    node_key,
                    max_depth=_GRAPH_OBSERVE_DEPTH,
                    max_nodes=_GRAPH_OBSERVE_MAX_NODES,
                    direction=_GRAPH_OBSERVE_DIRECTION,
                    access_scope=scope,
                    context=context,
                )
                for node_key in seeds
            )
        )
        if context.access.entitled_source_scopes is not None:
            walks = await self._entitled_walks(seeds, walks, context)
        nodes = {node.node_key: node for traversal in walks for node in traversal.nodes}
        relation_ids = {
            relation.relation_id for traversal in walks for relation in traversal.relations
        }
        return GraphObservation(
            nodes=len(nodes),
            relations=len(relation_ids),
            truncated=any(traversal.truncated for traversal in walks),
            # Every walk contributes documents, so a document only memory reached
            # is still summarised -- but ``related_results`` is built from the
            # result walks alone, because its ``result_id`` is contractually a
            # vector result's chunk id and a memory seed is not one.
            documents=_summarise_documents(
                tuple(nodes.values()),
                tuple(zip(results, walks[: len(results)], strict=True)),
            ),
        )

    async def _entitled_walks(
        self,
        seeds: Sequence[str],
        walks: Sequence[KnowledgeGraphTraversal],
        context: StorageOperationContext,
    ) -> list[KnowledgeGraphTraversal]:
        """The walks without what the reader's datasource entitlement leaves out.

        On a shared corpus ``graph_access_scope`` names no allowlist, so the graph is
        walked across every datasource; a reader narrowed to some of them is held here,
        by the same check triplet, path and subgraph search apply after their own walks.
        A walk whose seed the reader may not see contributes nothing, since a memory seed
        is the caller's word. A relation stays only when neither it nor either end is
        hidden, so a hidden node's key never reaches ``related_results``.
        """

        records: dict[str, GraphNodeRecord | GraphEdgeRecord] = {}
        for walk in walks:
            records.update((node.node_key, node) for node in walk.nodes)
            records.update((relation.relation_id, relation) for relation in walk.relations)
        states = await apply_graph_permissions(
            records, dict.fromkeys(records, "active"), self._authorizer, context
        )
        hidden = {key for key, state in states.items() if state == "denied"}
        return [_visible_walk(seed, walk, hidden) for seed, walk in zip(seeds, walks, strict=True)]


def _visible_walk(
    seed: str, walk: KnowledgeGraphTraversal, hidden: set[str]
) -> KnowledgeGraphTraversal:
    """``walk`` without its hidden records, or nothing at all when its seed is hidden.

    An empty walk keeps its place, so result walks still line up with their results.
    """

    if seed in hidden:
        return KnowledgeGraphTraversal(nodes=(), relations=())
    return KnowledgeGraphTraversal(
        nodes=tuple(node for node in walk.nodes if node.node_key not in hidden),
        relations=tuple(
            relation
            for relation in walk.relations
            if hidden.isdisjoint(
                (relation.relation_id, relation.source_node_key, relation.target_node_key)
            )
        ),
        truncated=walk.truncated,
    )


def _document_neighborhoods(
    seeded_traversals: Sequence[tuple[str, KnowledgeGraphTraversal]],
) -> dict[str, list[GraphResultNeighborhood]]:
    """Per document, which vector result reached it and the 2-hop slice that connects them.

    A relation with only one endpoint in the document (e.g. a cross-document ``LINKS_TO``)
    still belongs to that document's neighborhood -- it is how the result reached it. Requiring
    both endpoints would silently drop the connecting edge from both sides of the boundary.
    """

    related: dict[str, list[GraphResultNeighborhood]] = {}
    for result_id, traversal in seeded_traversals:
        nodes_by_key = {node.node_key: node for node in traversal.nodes}
        by_document: dict[str, list[GraphNodeRecord]] = {}
        for node in traversal.nodes:
            if node.document_id is None:
                continue
            by_document.setdefault(str(node.document_id), []).append(node)
        for key, doc_nodes in by_document.items():
            doc_node_keys = {node.node_key for node in doc_nodes}
            doc_relations = [
                relation
                for relation in traversal.relations
                if relation.source_node_key in doc_node_keys
                or relation.target_node_key in doc_node_keys
            ]
            boundary_keys = {
                other_key
                for relation in doc_relations
                for other_key in (relation.source_node_key, relation.target_node_key)
                if other_key not in doc_node_keys
            }
            neighborhood_nodes = [
                *doc_nodes,
                *(
                    nodes_by_key[boundary_key]
                    for boundary_key in boundary_keys
                    if boundary_key in nodes_by_key
                ),
            ]
            related.setdefault(key, []).append(
                GraphResultNeighborhood(
                    result_id=result_id,
                    nodes=tuple(compact_node(node) for node in neighborhood_nodes),
                    relations=tuple(compact_relation(relation) for relation in doc_relations),
                )
            )
    return related


def _summarise_documents(
    nodes: Sequence[GraphNodeRecord],
    seeded_traversals: Sequence[tuple[str, KnowledgeGraphTraversal]],
) -> tuple[GraphDocumentSummary, ...]:
    """Group traversal nodes by the document they belong to.

    Titles come from the DocumentVersion node and sections from Structure nodes; chunk
    nodes carry neither, which is why the grouping needs the graph rather than the
    vector payload. ``related_results`` replays, per document, which vector result
    reached it and the exact nodes/relations within its 2-hop walk that belong to it.
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

    related = _document_neighborhoods(seeded_traversals)
    return tuple(
        GraphDocumentSummary(
            document_id=key,
            title=titles.get(key),
            sections=tuple(sections[key]),
            related_results=tuple(related.get(key, ())),
        )
        for key in order
    )


__all__ = [
    "GraphDocumentSummary",
    "GraphObservation",
    "GraphObserver",
    "GraphResultNeighborhood",
]
