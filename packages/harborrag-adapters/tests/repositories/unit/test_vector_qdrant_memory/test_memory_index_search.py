"""Scope isolation, validity, type/limit filtering, and ranking of recall."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.errors import HarborStorageValidationError
from harborrag_adapters.repositories.vector.memory_index import QdrantMemoryIndex
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.ports.memory import MemoryQuery, MemoryScope, MemoryType

from ..test_vector_qdrant.fakes import ExtendedRawQdrant
from .conftest import (
    DIMENSIONS,
    EMBEDDING,
    RecordingEmbedder,
    hit,
    must_conditions,
    owner,
)


def _query(**overrides: object) -> MemoryQuery:
    fields: dict[str, object] = {"owner": owner(), "text": "what do I prefer?"}
    fields.update(overrides)
    return MemoryQuery(**fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (
            MemoryScope.SESSION,
            {"scope": "session", "tenant_id": "tenant-b", "user_id": "user-1"},
        ),
        (MemoryScope.USER, {"scope": "user", "tenant_id": "tenant-b", "user_id": "user-1"}),
        (
            MemoryScope.PROJECT,
            {"scope": "project", "tenant_id": "tenant-b", "project_id": "proj-1"},
        ),
        (MemoryScope.TENANT, {"scope": "tenant", "tenant_id": "tenant-b"}),
    ],
)
async def test_each_scope_filters_on_exactly_its_owner_fields(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
    scope: MemoryScope,
    expected: dict[str, str],
) -> None:
    await index.search_memories(_query(owner=owner(tenant_id="tenant-b"), scopes=(scope,)))

    # A read follows the caller's own tenant collection, never a constant one.
    assert raw.query_calls[0]["collection_name"] == "tenant-b_memories"
    conditions = must_conditions(raw.query_calls[0])
    assert conditions.pop("invalid_at:is_null") is True
    if scope is MemoryScope.SESSION:
        assert conditions.pop("session_id") == "session-1"
    assert conditions == expected


@pytest.mark.asyncio
async def test_a_caller_missing_a_required_field_never_searches_that_scope(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    anonymous = owner(user_id=None, session_id=None, run_id=None, project_id=None)

    matches = await index.search_memories(
        _query(
            owner=anonymous,
            scopes=(MemoryScope.RUN, MemoryScope.SESSION, MemoryScope.USER, MemoryScope.TENANT),
        )
    )

    assert matches == ()
    assert [must_conditions(call)["scope"] for call in raw.query_calls] == ["tenant"]


@pytest.mark.asyncio
async def test_every_scope_is_searched_when_the_query_names_none(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    await index.search_memories(_query())

    assert [must_conditions(call)["scope"] for call in raw.query_calls] == [
        "run",
        "session",
        "user",
        "project",
        "tenant",
        "global",
    ]


@pytest.mark.asyncio
async def test_invalidated_memories_are_excluded_unless_include_invalid(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    await index.search_memories(_query(scopes=(MemoryScope.USER,), include_invalid=True))

    assert "invalid_at:is_null" not in must_conditions(raw.query_calls[0])


@pytest.mark.asyncio
async def test_memory_types_and_limit_reach_the_provider(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    await index.search_memories(
        _query(
            scopes=(MemoryScope.USER,),
            memory_types=(MemoryType.FACT, MemoryType.PREFERENCE),
            limit=7,
        )
    )

    call = raw.query_calls[0]
    assert must_conditions(call)["memory_type"] == ["fact", "preference"]
    assert call["limit"] == 7
    assert call["query"] == EMBEDDING


@pytest.mark.asyncio
async def test_matches_are_deduplicated_ranked_and_truncated(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    # Cosine normalization is monotonic, so provider order does not decide rank.
    raw.points = [hit("mem-mid", 0.6), hit("mem-top", 1.0), hit("mem-low", 0.2)]

    matches = await index.search_memories(
        _query(scopes=(MemoryScope.USER, MemoryScope.TENANT), limit=3)
    )

    assert [item.memory_id for item in matches] == ["mem-top", "mem-mid", "mem-low"]
    assert [item.score for item in matches] == [1.0, 0.8, 0.6]

    truncated = await index.search_memories(
        _query(scopes=(MemoryScope.USER, MemoryScope.TENANT), limit=2)
    )
    assert [item.memory_id for item in truncated] == ["mem-top", "mem-mid"]


@pytest.mark.asyncio
async def test_hits_without_a_payload_identity_are_dropped(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    anonymous = hit("mem-1", 1.0)
    anonymous.payload = {}
    raw.points = [anonymous, hit("mem-2", 0.6)]

    matches = await index.search_memories(_query(scopes=(MemoryScope.USER,)))

    assert [item.memory_id for item in matches] == ["mem-2"]


@pytest.mark.asyncio
async def test_search_embeds_the_query_text_once(
    index: QdrantMemoryIndex,
    embedder: RecordingEmbedder,
) -> None:
    await index.search_memories(_query(scopes=(MemoryScope.USER, MemoryScope.TENANT)))

    assert embedder.texts == ["what do I prefer?"]


@pytest.mark.asyncio
async def test_search_without_text_or_embedding_is_a_validation_error(
    repository: QdrantVectorRepository,
    raw: ExtendedRawQdrant,
) -> None:
    index = QdrantMemoryIndex(repository, dimensions=DIMENSIONS)

    with pytest.raises(HarborStorageValidationError, match="requires an embedding"):
        await index.search_memories(_query(text=None))

    assert raw.query_calls == []
