"""Permission-scoped evidence, entity, assertion, and semantic path reads."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import uuid4

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.ports.storage import VectorRepositoryPort
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention
from harborrag_core.topology.search import TopologySearchPort
from harborrag_engine.retrieval import ActiveVersionCandidateValidator

from ..contracts import (
    EntityResolveResponse,
    EvidenceFetchResponse,
    RelationSearchResponse,
    SemanticPathRequest,
    SemanticPathResponse,
)
from .permissions import RetrievalPermissions
from .validation import required_text

_MAX_TOPOLOGY_READ = 256


@dataclass(frozen=True, slots=True)
class _Route:
    entity_id: str
    assertions: tuple[CanonicalAssertion, ...] = ()
    visited: frozenset[str] = frozenset()


class KnowledgeRetrieval:
    """Read canonical semantic data and resolve every result to source evidence."""

    def __init__(
        self,
        topology: TopologySearchPort | None,
        vectors: VectorRepositoryPort,
        validator: ActiveVersionCandidateValidator,
        permissions: RetrievalPermissions,
    ) -> None:
        self._topology = topology
        self._vectors = vectors
        self._validator = validator
        self._permissions = permissions

    async def fetch(
        self,
        chunk_ids: tuple[str, ...],
        *,
        access: AccessContext,
    ) -> EvidenceFetchResponse:
        request_id = f"evidence-{uuid4().hex}"
        context = _context(access, request_id, "evidence-fetch")
        identity = DocumentIdentityBuilder()
        point_ids = tuple(identity.point_id(chunk_id=item) for item in chunk_ids)
        records = await self._vectors.get_records("evidence", point_ids, context=context)
        candidates = tuple(
            VectorSearchResult(id=row.id, score=1.0, raw_score=1.0, payload=row.payload)
            for row in records
            if str(row.tenant_id) == str(access.tenant_id)
            and row.id == identity.point_id(chunk_id=str(row.payload.get("chunk_id", "")))
        )
        active = await self._validator.validate(candidates)
        permitted = await self._permissions.validate(active.accepted, context)
        by_chunk = {str(item.payload.get("chunk_id")): _result(item) for item in permitted}
        return EvidenceFetchResponse(
            request_id,
            tuple(by_chunk[item] for item in chunk_ids if item in by_chunk),
            tuple(item for item in chunk_ids if item not in by_chunk),
        )

    async def resolve_entities(
        self,
        name: str,
        *,
        limit: int,
        access: AccessContext,
    ) -> EntityResolveResponse:
        topology = self._require_topology()
        request_id = f"entity-{uuid4().hex}"
        rows = await topology.active_mentions(
            str(access.tenant_id),
            labels=(name,),
            limit=min(_MAX_TOPOLOGY_READ, limit + 1),
            access=access,
        )
        mentions = _one_mention_per_entity(rows)
        return EntityResolveResponse(
            request_id,
            mentions[:limit],
            truncated=len(mentions) > limit,
        )

    async def lookup_entities(
        self,
        *,
        entity_ids: tuple[str, ...] = (),
        chunk_ids: tuple[str, ...] = (),
        access: AccessContext,
    ) -> tuple[CanonicalMention, ...]:
        topology = self._require_topology()
        if not entity_ids and not chunk_ids:
            return ()
        rows = await topology.active_mentions(
            str(access.tenant_id),
            entity_ids=entity_ids,
            chunk_ids=chunk_ids,
            limit=_MAX_TOPOLOGY_READ,
            access=access,
        )
        return _one_mention_per_entity(rows)

    async def find_relations(
        self,
        entity_id: str,
        *,
        predicates: tuple[str, ...],
        direction: str,
        limit: int,
        access: AccessContext,
    ) -> RelationSearchResponse:
        topology = self._require_topology()
        request_id = f"relation-{uuid4().hex}"
        rows = await topology.active_assertions(
            str(access.tenant_id),
            entity_ids=(entity_id,),
            limit=_MAX_TOPOLOGY_READ,
            access=access,
        )
        selected = tuple(
            item
            for item in rows
            if (not predicates or item.observation.predicate in predicates)
            and _matches_direction(item, entity_id, direction)
        )
        visible = selected[:limit]
        mentions = await self._mentions_for_assertions(topology, visible, access)
        return RelationSearchResponse(
            request_id,
            visible,
            mentions,
            truncated=len(selected) > limit or len(rows) >= _MAX_TOPOLOGY_READ,
        )

    async def find_paths(
        self,
        request: SemanticPathRequest,
    ) -> SemanticPathResponse:
        topology = self._require_topology()
        request_id = f"path-{uuid4().hex}"
        queue = deque(
            [
                _Route(
                    request.start_entity_id,
                    visited=frozenset({request.start_entity_id}),
                )
            ]
        )
        paths: list[tuple[CanonicalAssertion, ...]] = []
        truncated = False
        while queue and len(paths) < request.limit:
            route = queue.popleft()
            if len(route.assertions) >= request.max_hops:
                continue
            assertions = await topology.active_assertions(
                str(request.access.tenant_id),
                entity_ids=(route.entity_id,),
                limit=_MAX_TOPOLOGY_READ,
                access=request.access,
            )
            truncated |= len(assertions) >= _MAX_TOPOLOGY_READ
            for assertion in assertions:
                if request.predicates and assertion.observation.predicate not in request.predicates:
                    continue
                next_id = _next_entity(assertion, route.entity_id, request.traversal)
                if next_id is None or next_id in route.visited:
                    continue
                extended = (*route.assertions, assertion)
                if next_id == request.end_entity_id:
                    paths.append(extended)
                    if len(paths) == request.limit:
                        truncated |= bool(queue)
                        break
                    continue
                queue.append(_Route(next_id, extended, route.visited | frozenset({next_id})))
        assertions = tuple(item for path in paths for item in path)
        mentions = await self._mentions_for_assertions(topology, assertions, request.access)
        return SemanticPathResponse(request_id, tuple(paths), mentions, truncated)

    async def _mentions_for_assertions(
        self,
        topology: TopologySearchPort,
        assertions: Iterable[CanonicalAssertion],
        access: AccessContext,
    ) -> tuple[CanonicalMention, ...]:
        entity_ids = tuple(
            dict.fromkeys(
                entity
                for item in assertions
                for entity in (item.subject_entity_id, item.object_entity_id)
            )
        )
        if not entity_ids:
            return ()
        rows = await topology.active_mentions(
            str(access.tenant_id),
            entity_ids=entity_ids,
            limit=_MAX_TOPOLOGY_READ,
            access=access,
        )
        return _one_mention_per_entity(rows)

    def _require_topology(self) -> TopologySearchPort:
        if self._topology is None:
            raise HarborCapabilityError("semantic topology retrieval is not configured")
        return self._topology


def _one_mention_per_entity(
    mentions: Iterable[CanonicalMention],
) -> tuple[CanonicalMention, ...]:
    output: dict[str, CanonicalMention] = {}
    for mention in mentions:
        current = output.get(mention.entity_id)
        if current is None or _mention_quality(mention) > _mention_quality(current):
            output[mention.entity_id] = mention
    return tuple(output.values())


def _mention_quality(mention: CanonicalMention) -> tuple[int, int, str]:
    value = mention.observation
    return (len(value.description.split()), len(value.aliases), value.name.casefold())


def _matches_direction(
    assertion: CanonicalAssertion,
    entity_id: str,
    direction: str,
) -> bool:
    return (
        direction == "either"
        or (direction == "outgoing" and assertion.subject_entity_id == entity_id)
        or (direction == "incoming" and assertion.object_entity_id == entity_id)
    )


def _next_entity(
    assertion: CanonicalAssertion,
    entity_id: str,
    traversal: str,
) -> str | None:
    if assertion.subject_entity_id == entity_id:
        return assertion.object_entity_id
    if traversal == "either" and assertion.object_entity_id == entity_id:
        return assertion.subject_entity_id
    return None


def _context(
    access: AccessContext,
    request_id: str,
    operation: str,
) -> StorageOperationContext:
    return StorageOperationContext.for_access(
        access,
        operation_kind=operation,
        idempotency_key=request_id,
    )


def _result(candidate: VectorSearchResult) -> RetrievalResult:
    payload = candidate.payload
    metadata: dict[str, object] = {
        "document_id": required_text(payload, "document_id"),
        "document_version_id": required_text(payload, "document_version_id"),
        "record_kind": required_text(payload, "record_kind"),
        "chunk_kind": required_text(payload, "chunk_kind"),
        "connector_type": required_text(payload, "connector_type"),
        "citation_locator": payload.get("citation_locator", {}),
        "quality_score": payload.get("quality_score"),
        "raw_score": candidate.raw_score,
        "retrieval_source": "qdrant-authoritative",
    }
    for key in (
        "source_scope_id",
        "source_item_id",
        "document_title",
        "section_path",
        "content_hash",
    ):
        value = payload.get(key)
        if value is not None:
            metadata[key] = value
    return RetrievalResult(
        id=required_text(payload, "chunk_id"),
        text=required_text(payload, "content"),
        score=candidate.score,
        metadata=metadata,
    )


__all__ = ["KnowledgeRetrieval"]
