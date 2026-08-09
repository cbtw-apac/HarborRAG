"""Authoritative graph retrieval over rebuildable projections."""

from __future__ import annotations

from asyncio import gather
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
)
from harborrag_core.ports import GraphRetrievalRepositoryPort
from harborrag_core.retrieval import (
    GraphNeighborhoodQuery,
    GraphPath,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
)
from harborrag_core.storage import StorageOperationContext

from .active_versions import ActiveVersionResolver

# Stale and unpublished records are dropped *after* the store has answered, so asking for
# exactly what the caller wants yields fewer than that whenever the neighborhood contains
# superseded versions. Widening the request first keeps the shortfall a property of the
# graph rather than of the filter. The ceiling matches the le=100 bound on every query
# model, so a widened request can never exceed what the contract allows.
_CANDIDATE_MULTIPLIER = 4
_MAX_CANDIDATES = 100


def _candidate_limit(requested: int) -> int:
    return min(_MAX_CANDIDATES, max(requested, requested * _CANDIDATE_MULTIPLIER))


@dataclass(frozen=True, slots=True)
class GraphSearchDiagnostics:
    candidate_count: int
    accepted_count: int
    stale_count: int
    unpublished_count: int
    projection_truncated: bool


@dataclass(frozen=True, slots=True)
class AuthoritativeTripletResult:
    triplets: tuple[GraphTriplet, ...]
    diagnostics: GraphSearchDiagnostics


@dataclass(frozen=True, slots=True)
class AuthoritativePathResult:
    paths: tuple[GraphPath, ...]
    diagnostics: GraphSearchDiagnostics


@dataclass(frozen=True, slots=True)
class AuthoritativeSubgraphResult:
    graph: KnowledgeGraphTraversal
    diagnostics: GraphSearchDiagnostics


class AuthoritativeGraphSearch:
    """Reject graph records whose document versions are not active in Postgres."""

    def __init__(
        self,
        repository: GraphRetrievalRepositoryPort,
        active_versions: ActiveVersionResolver,
    ) -> None:
        self._repository = repository
        self._active_versions = active_versions

    async def triplets(
        self,
        query: GraphTripletQuery,
        *,
        context: StorageOperationContext,
    ) -> AuthoritativeTripletResult:
        candidate_limit = _candidate_limit(query.limit)
        candidates = await self._repository.search_triplets(
            query.model_copy(update={"limit": candidate_limit}),
            context=context,
        )
        visibility = await self._visibility(
            tuple(
                node
                for triplet in candidates.triplets
                for node in (triplet.subject, triplet.object)
            )
        )
        accepted: list[GraphTriplet] = []
        stale = unpublished = 0
        for triplet in candidates.triplets:
            state = self._item_state(
                (triplet.subject, triplet.object),
                (triplet.predicate,),
                visibility,
            )
            if state == "active":
                accepted.append(triplet)
            elif state == "unpublished":
                unpublished += 1
            else:
                stale += 1
        selected = tuple(accepted[: query.limit])
        return AuthoritativeTripletResult(
            triplets=selected,
            diagnostics=GraphSearchDiagnostics(
                candidate_count=len(candidates.triplets),
                accepted_count=len(selected),
                stale_count=stale,
                unpublished_count=unpublished,
                projection_truncated=candidates.truncated or len(accepted) > query.limit,
            ),
        )

    async def paths(
        self,
        query: GraphPathQuery,
        *,
        context: StorageOperationContext,
    ) -> AuthoritativePathResult:
        candidate_limit = _candidate_limit(query.max_paths)
        candidates = await self._repository.find_paths(
            query.model_copy(update={"max_paths": candidate_limit}),
            context=context,
        )
        visibility = await self._visibility(
            tuple(node for path in candidates.paths for node in path.nodes)
        )
        accepted: list[GraphPath] = []
        stale = unpublished = 0
        for path in candidates.paths:
            state = self._item_state(path.nodes, path.relations, visibility)
            if state == "active":
                accepted.append(path)
            elif state == "unpublished":
                unpublished += 1
            else:
                stale += 1
        selected = tuple(accepted[: query.max_paths])
        return AuthoritativePathResult(
            paths=selected,
            diagnostics=GraphSearchDiagnostics(
                candidate_count=len(candidates.paths),
                accepted_count=len(selected),
                stale_count=stale,
                unpublished_count=unpublished,
                projection_truncated=candidates.truncated or len(accepted) > query.max_paths,
            ),
        )

    async def subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        context: StorageOperationContext,
    ) -> AuthoritativeSubgraphResult:
        candidates = await self._repository.expand_subgraph(
            query.model_copy(update={"max_nodes": _candidate_limit(query.max_nodes)}),
            context=context,
        )
        return await self._accept_subgraph(candidates, max_nodes=query.max_nodes)

    async def neighborhood(
        self,
        seeds: Sequence[str],
        query: GraphNeighborhoodQuery,
        *,
        context: StorageOperationContext,
    ) -> AuthoritativeSubgraphResult:
        """Merge the expansions of several seeds into one deduplicated neighborhood.

        Seeds are resolved by the caller, because only the vector index turns free text
        into node keys and this layer holds no vector repository.
        """

        if not seeds:
            return AuthoritativeSubgraphResult(
                graph=KnowledgeGraphTraversal(nodes=(), relations=()),
                diagnostics=GraphSearchDiagnostics(
                    candidate_count=0,
                    accepted_count=0,
                    stale_count=0,
                    unpublished_count=0,
                    projection_truncated=False,
                ),
            )
        expansions = await gather(
            *(
                self._repository.expand_subgraph(
                    query.to_subgraph_query(seed).model_copy(
                        update={"max_nodes": _candidate_limit(query.max_nodes)}
                    ),
                    context=context,
                )
                for seed in seeds
            )
        )
        merged_nodes: dict[str, GraphNodeRecord] = {}
        merged_relations: dict[str, GraphEdgeRecord] = {}
        for expansion in expansions:
            for node in expansion.nodes:
                merged_nodes.setdefault(node.node_key, node)
            for relation in expansion.relations:
                merged_relations.setdefault(relation.relation_id, relation)
        return await self._accept_subgraph(
            KnowledgeGraphTraversal(
                nodes=tuple(merged_nodes.values()),
                relations=tuple(merged_relations.values()),
                truncated=any(expansion.truncated for expansion in expansions),
            ),
            max_nodes=query.max_nodes,
        )

    async def _accept_subgraph(
        self,
        candidates: KnowledgeGraphTraversal,
        *,
        max_nodes: int,
    ) -> AuthoritativeSubgraphResult:
        visibility = await self._visibility(candidates.nodes)
        active_nodes = tuple(
            node for node in candidates.nodes if visibility.get(node.node_key) == "active"
        )
        accepted_nodes = active_nodes[:max_nodes]
        accepted_keys = {node.node_key for node in accepted_nodes}
        nodes_by_key = {node.node_key: node for node in accepted_nodes}
        accepted_relations = tuple(
            relation
            for relation in candidates.relations
            if relation.source_node_key in accepted_keys
            and relation.target_node_key in accepted_keys
            and self._relation_matches_nodes(relation, nodes_by_key)
        )
        stale = sum(state == "stale" for state in visibility.values())
        unpublished = sum(state == "unpublished" for state in visibility.values())
        # Truncation now means "the graph holds more than you were given", which is what a
        # caller needs to decide whether to widen. Rejected nodes alone do not set it: the
        # request was widened before filtering, so a short result that was not cut means
        # the neighborhood really is that small, and stale_count reports the rejections.
        truncated = candidates.truncated or len(active_nodes) > max_nodes
        return AuthoritativeSubgraphResult(
            graph=KnowledgeGraphTraversal(
                nodes=accepted_nodes,
                relations=accepted_relations,
                truncated=truncated,
            ),
            diagnostics=GraphSearchDiagnostics(
                candidate_count=len(candidates.nodes),
                accepted_count=len(accepted_nodes),
                stale_count=stale,
                unpublished_count=unpublished,
                projection_truncated=truncated,
            ),
        )

    async def _visibility(
        self,
        nodes: Sequence[GraphNodeRecord],
    ) -> dict[str, str]:
        unique = {node.node_key: node for node in nodes}
        document_ids = tuple(
            dict.fromkeys(
                str(node.document_id)
                for node in unique.values()
                if node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
                and node.document_id is not None
            )
        )
        active = await self._active_versions.active_versions(document_ids)
        return {node.node_key: self._node_state(node, active) for node in unique.values()}

    @staticmethod
    def _node_state(
        node: GraphNodeRecord,
        active: Mapping[str, ActiveDocumentVersion],
    ) -> str:
        if node.ownership_scope != GraphOwnershipScope.DOCUMENT_VERSION:
            return "active"
        version = active.get(str(node.document_id))
        if version is None:
            return "unpublished"
        if str(version.document_version_id) != str(node.document_version_id):
            return "stale"
        return "active"

    @classmethod
    def _item_state(
        cls,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        visibility: Mapping[str, str],
    ) -> str:
        states = {visibility.get(node.node_key, "unpublished") for node in nodes}
        if "unpublished" in states:
            return "unpublished"
        if "stale" in states:
            return "stale"
        nodes_by_key = {node.node_key: node for node in nodes}
        if any(not cls._relation_matches_nodes(relation, nodes_by_key) for relation in relations):
            return "stale"
        return "active"

    @staticmethod
    def _relation_matches_nodes(
        relation: GraphEdgeRecord,
        nodes: Mapping[str, GraphNodeRecord],
    ) -> bool:
        if relation.ownership_scope != GraphOwnershipScope.DOCUMENT_VERSION:
            return True
        endpoints = (
            nodes.get(relation.source_node_key),
            nodes.get(relation.target_node_key),
        )
        versions = {
            str(node.document_version_id)
            for node in endpoints
            if node is not None and node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
        }
        return str(relation.document_version_id) in versions
