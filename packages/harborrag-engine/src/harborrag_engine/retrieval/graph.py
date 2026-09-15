"""Authoritative graph retrieval over rebuildable projections."""

from __future__ import annotations

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
    GraphDirection,
    GraphPath,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
)
from harborrag_core.storage import StorageOperationContext

from .active_versions import ActiveVersionResolver
from .graph_metadata import selector_matches, supported_nodes
from .graph_visibility import (
    GraphVisibilityAuthorizer,
    apply_graph_permissions,
    reachable_subgraph,
)

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
        authorizer: GraphVisibilityAuthorizer | None = None,
    ) -> None:
        self._repository = repository
        self._active_versions = active_versions
        self._authorizer = authorizer

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
            ),
            tuple(triplet.predicate for triplet in candidates.triplets),
            context=context,
        )
        accepted: list[GraphTriplet] = []
        stale = unpublished = 0
        visible_candidates = 0
        for triplet in candidates.triplets:
            state = self._item_state(
                (triplet.subject, triplet.object),
                (triplet.predicate,),
                visibility,
            )
            if state == "denied":
                continue
            visible_candidates += 1
            if state == "active":
                subject, object_node = supported_nodes(
                    (triplet.subject, triplet.object), (triplet.predicate,)
                )
                if selector_matches(subject, query.subject) and selector_matches(
                    object_node, query.object
                ):
                    accepted.append(
                        triplet.model_copy(update={"subject": subject, "object": object_node})
                    )
            elif state == "unpublished":
                unpublished += 1
            else:
                stale += 1
        selected = tuple(accepted[: query.limit])
        return AuthoritativeTripletResult(
            triplets=selected,
            diagnostics=GraphSearchDiagnostics(
                candidate_count=visible_candidates,
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
            tuple(node for path in candidates.paths for node in path.nodes),
            tuple(relation for path in candidates.paths for relation in path.relations),
            context=context,
        )
        accepted: list[GraphPath] = []
        stale = unpublished = 0
        visible_candidates = 0
        for path in candidates.paths:
            state = self._item_state(path.nodes, path.relations, visibility)
            if state == "denied":
                continue
            visible_candidates += 1
            if state == "active":
                nodes = supported_nodes(path.nodes, path.relations)
                if selector_matches(nodes[0], query.start_node) and selector_matches(
                    nodes[-1], query.end_node
                ):
                    accepted.append(path.model_copy(update={"nodes": nodes}))
            elif state == "unpublished":
                unpublished += 1
            else:
                stale += 1
        selected = tuple(accepted[: query.max_paths])
        return AuthoritativePathResult(
            paths=selected,
            diagnostics=GraphSearchDiagnostics(
                candidate_count=visible_candidates,
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
        return await self._accept_subgraph(
            candidates,
            max_nodes=query.max_nodes,
            selector=query.start_node,
            direction=query.direction,
            context=context,
        )

    async def _accept_subgraph(
        self,
        candidates: KnowledgeGraphTraversal,
        *,
        max_nodes: int,
        selector: str,
        direction: GraphDirection,
        context: StorageOperationContext,
    ) -> AuthoritativeSubgraphResult:
        visibility = await self._visibility(
            candidates.nodes, candidates.relations, context=context
        )
        active_nodes = tuple(
            node for node in candidates.nodes if visibility.get(node.node_key) == "active"
        )
        active_relations = tuple(
            relation
            for relation in candidates.relations
            if visibility.get(relation.relation_id) == "active"
        )
        described_nodes = supported_nodes(active_nodes, active_relations)
        accepted_nodes, accepted_relations, reachable_count = reachable_subgraph(
            described_nodes,
            active_relations,
            selector=selector,
            direction=direction,
            max_nodes=max_nodes,
        )
        stale = sum(visibility[node.node_key] == "stale" for node in candidates.nodes)
        unpublished = sum(visibility[node.node_key] == "unpublished" for node in candidates.nodes)
        # Truncation now means "the graph holds more than you were given", which is what a
        # caller needs to decide whether to widen. Rejected nodes alone do not set it: the
        # request was widened before filtering, so a short result that was not cut means
        # the neighborhood really is that small, and stale_count reports the rejections.
        truncated = candidates.truncated or reachable_count > max_nodes
        return AuthoritativeSubgraphResult(
            graph=KnowledgeGraphTraversal(
                nodes=accepted_nodes,
                relations=accepted_relations,
                truncated=truncated,
            ),
            diagnostics=GraphSearchDiagnostics(
                candidate_count=sum(
                    visibility[node.node_key] != "denied" for node in candidates.nodes
                ),
                accepted_count=len(accepted_nodes),
                stale_count=stale,
                unpublished_count=unpublished,
                projection_truncated=truncated,
            ),
        )

    async def _visibility(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> dict[str, str]:
        unique: dict[str, GraphNodeRecord | GraphEdgeRecord] = {
            node.node_key: node for node in nodes
        }
        unique.update({relation.relation_id: relation for relation in relations})
        document_ids = tuple(
            dict.fromkeys(
                str(node.document_id)
                for node in unique.values()
                if node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
                and node.document_id is not None
            )
        )
        active = await self._active_versions.active_versions(document_ids)
        states = {key: self._node_state(record, active) for key, record in unique.items()}
        return await apply_graph_permissions(
            unique, states, self._authorizer, context
        )

    @staticmethod
    def _node_state(
        node: GraphNodeRecord | GraphEdgeRecord,
        active: Mapping[str, ActiveDocumentVersion],
    ) -> str:
        if node.ownership_scope != GraphOwnershipScope.DOCUMENT_VERSION:
            if (
                isinstance(node, GraphEdgeRecord)
                and node.relation_type.value != "has_data_source"
                and node.attributes.get("logical_view") is not True
            ):
                # Old source-owned assertions have no independently verifiable
                # support. Fail closed until their scope is rebuilt with supports.
                return "stale"
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
        states.update(visibility.get(relation.relation_id, "unpublished") for relation in relations)
        if "denied" in states:
            return "denied"
        if "unpublished" in states:
            return "unpublished"
        if "stale" in states:
            return "stale"
        return "active"
