"""Portable schema/payload/filter mapping for the ``memories`` vector index.

Everything here speaks only the core vector schemas, so the mapping is
provider-independent and testable without a Qdrant client. The scope filter
is derived from ``scope_owner_fields`` -- the same function
``harborrag_core.ports.memory.visible_to`` uses -- so an index query can
never widen visibility beyond what the pure predicate allows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from harborrag_core.indexing import (
    FilterOperator,
    VectorDistance,
    VectorFilter,
    VectorFilterCondition,
    VectorIndexSpec,
    VectorSearchResult,
)
from harborrag_core.ports.memory import (
    Memory,
    MemoryMatch,
    MemoryQuery,
    MemoryScope,
    scope_owner_fields,
)

MEMORY_INDEX = "memories"
"""Logical index name: memories live apart from evidence, they expire apart too."""

# Keyword-indexed payload fields. ``QdrantCollectionMixin`` creates every
# metadata index as KEYWORD, so only the string-valued filter fields belong
# here -- ``importance`` and the timestamps are payload, never keywords.
_PAYLOAD_INDEXES = (
    "memory_id",
    "scope",
    "memory_type",
    "tenant_id",
    "user_id",
    "project_id",
    "session_id",
    "run_id",
)


def memory_index_spec(
    *,
    dimension: int,
    distance: VectorDistance = VectorDistance.COSINE,
) -> VectorIndexSpec:
    """Dense-only spec for one tenant's ``memories`` collection."""

    return VectorIndexSpec(
        index_name=MEMORY_INDEX,
        dimension=dimension,
        distance=distance,
        tenant_scoped=True,
        metadata_indexes=list(_PAYLOAD_INDEXES),
    )


def _instant(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def memory_payload(memory: Memory) -> dict[str, Any]:
    """Index payload for one memory: identity, scope keys, and lifecycle only.

    The content itself is deliberately absent -- Postgres is the system of
    record, and keeping the text out of the index means erasure only has to
    delete a point. Every key is written even when its value is ``None`` so
    the ``invalid_at is null`` validity filter has a field to match.
    """

    owner = memory.owner
    return {
        "memory_id": memory.memory_id,
        "scope": memory.scope.value,
        "memory_type": memory.memory_type.value,
        "tenant_id": owner.tenant_id,
        "user_id": owner.user_id,
        "project_id": owner.project_id,
        "session_id": owner.session_id,
        "run_id": owner.run_id,
        "importance": memory.importance,
        "valid_from": _instant(memory.valid_from),
        "invalid_at": _instant(memory.invalid_at),
        "updated_at": _instant(memory.updated_at),
        "content_hash": memory.content_hash,
    }


def _equals(field: str, value: str) -> VectorFilterCondition:
    return VectorFilterCondition(field=field, operator=FilterOperator.EQUALS, value=value)


def scope_filter(scope: MemoryScope, query: MemoryQuery) -> VectorFilter | None:
    """Filter selecting exactly the memories at ``scope`` visible to the caller.

    Returns ``None`` when the caller lacks a field the scope requires (for
    example searching without a ``run_id``): such a caller can never see a
    memory at that scope, so the scope is skipped rather than searched with a
    weaker predicate. ``tenant_id`` is always asserted, even though each
    tenant has its own physical collection and even for ``GLOBAL`` memories,
    which contribute no owner fields of their own.
    """

    owner = query.owner
    must = [_equals("scope", scope.value), _equals("tenant_id", owner.tenant_id)]
    for name in scope_owner_fields(scope):
        if name == "tenant_id":
            continue
        value = getattr(owner, name)
        if value is None:
            return None
        must.append(_equals(name, value))
    if query.memory_types:
        must.append(
            VectorFilterCondition(
                field="memory_type",
                operator=FilterOperator.IN,
                value=[item.value for item in query.memory_types],
            )
        )
    if not query.include_invalid:
        must.append(
            VectorFilterCondition(
                field="invalid_at",
                operator=FilterOperator.EXISTS,
                value=False,
            )
        )
    return VectorFilter(must=must)


def query_scopes(query: MemoryQuery) -> tuple[MemoryScope, ...]:
    """Scopes to search: the query's own, or every scope when unspecified."""

    return query.scopes or tuple(MemoryScope)


def match_of(result: VectorSearchResult) -> MemoryMatch | None:
    """Build a match from a hit's payload identity, or ``None`` if it has none.

    ``VectorSearchResult.id`` is the provider point id (a UUID derived from
    the memory id), so the canonical identity has to come from the payload.
    """

    memory_id = result.payload.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id.strip():
        return None
    return MemoryMatch(memory_id=memory_id, score=result.score)


def ranked_matches(
    scores: dict[str, float],
    *,
    limit: int,
) -> tuple[MemoryMatch, ...]:
    """Order fused hits by descending score, memory id breaking ties."""

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return tuple(MemoryMatch(memory_id=key, score=value) for key, value in ordered[:limit])


__all__ = [
    "MEMORY_INDEX",
    "match_of",
    "memory_index_spec",
    "memory_payload",
    "query_scopes",
    "ranked_matches",
    "scope_filter",
]
