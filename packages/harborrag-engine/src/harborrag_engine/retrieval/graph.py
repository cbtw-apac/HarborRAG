"""Authoritative graph retrieval over rebuildable projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    GraphEdgeRecord,
    GraphNodeRecord,
    KnowledgeGraphTraversal,
)
from harborrag_core.ports import GraphRetrievalRepositoryPort
from harborrag_core.retrieval import (
    GraphPath,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
)
from harborrag_core.storage import StorageOperationContext

from .active_versions import ActiveVersionResolver


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
        candidate_limit = min(100, max(query.limit, query.limit * 4))
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
        candidate_limit = min(100, max(query.max_paths, query.max_paths * 4))
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
        candidates = await self._repository.expand_subgraph(query, context=context)
        visibility = await self._visibility(candidates.nodes)
        accepted_nodes = tuple(
            node for node in candidates.nodes if visibility.get(node.node_key) == "active"
        )
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
        return AuthoritativeSubgraphResult(
            graph=KnowledgeGraphTraversal(
                nodes=accepted_nodes,
                relations=accepted_relations,
                truncated=candidates.truncated,
            ),
            diagnostics=GraphSearchDiagnostics(
                candidate_count=len(candidates.nodes),
                accepted_count=len(accepted_nodes),
                stale_count=stale,
                unpublished_count=unpublished,
                projection_truncated=candidates.truncated,
            ),
        )

    async def _visibility(
        self,
        nodes: Sequence[GraphNodeRecord],
    ) -> dict[str, str]:
        unique = {node.node_key: node for node in nodes}
        document_ids = tuple(dict.fromkeys(str(node.document_id) for node in unique.values()))
        active = await self._active_versions.active_versions(document_ids)
        return {node.node_key: self._node_state(node, active) for node in unique.values()}

    @staticmethod
    def _node_state(
        node: GraphNodeRecord,
        active: Mapping[str, ActiveDocumentVersion],
    ) -> str:
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
        endpoints = (
            nodes.get(relation.source_node_key),
            nodes.get(relation.target_node_key),
        )
        versions = {str(node.document_version_id) for node in endpoints if node is not None}
        return str(relation.document_version_id) in versions
