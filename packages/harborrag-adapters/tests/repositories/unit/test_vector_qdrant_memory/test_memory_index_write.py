"""Collection provisioning, payload shape, deletion, and error normalization."""

from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageError,
    HarborStorageValidationError,
    HarborVectorDimensionError,
)
from harborrag_adapters.repositories.vector.memory_index import MEMORY_INDEX, QdrantMemoryIndex
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.ports.memory import MemoryScope

from ..test_vector_qdrant.fakes import ExtendedRawQdrant
from .conftest import DIMENSIONS, EMBEDDING, RecordingEmbedder, memory, owner


@pytest.mark.asyncio
async def test_memories_live_in_their_own_tenant_scoped_collection(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    await index.index_memory(memory())

    assert MEMORY_INDEX == "memories"
    assert [call["collection_name"] for call in raw.create_collection_calls] == [
        "tenant-a_memories"
    ]
    assert raw.upsert_calls[0]["collection_name"] == "tenant-a_memories"


@pytest.mark.asyncio
async def test_collection_is_ensured_once_per_tenant(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    await index.index_memory(memory(memory_id="mem-1"))
    await index.index_memory(memory(memory_id="mem-2"))
    await index.delete_memory(owner(), "mem-1")
    other_tenant = owner(tenant_id="tenant-b")
    await index.index_memory(memory(memory_id="mem-3", owner=other_tenant))

    assert [call["collection_name"] for call in raw.create_collection_calls] == [
        "tenant-a_memories",
        "tenant-b_memories",
    ]


@pytest.mark.asyncio
async def test_collection_is_dense_only_with_keyword_payload_indexes(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    await index.index_memory(memory())

    options = raw.create_collection_calls[0]
    assert "sparse_vectors_config" not in options
    assert options["vectors_config"].size == DIMENSIONS
    assert {call["field_name"] for call in raw.create_payload_index_calls} == {
        "memory_id",
        "scope",
        "memory_type",
        "tenant_id",
        "user_id",
        "project_id",
        "session_id",
        "run_id",
    }


@pytest.mark.asyncio
async def test_payload_carries_scope_keys_and_never_the_content(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
    embedder: RecordingEmbedder,
) -> None:
    stored = memory(scope=MemoryScope.SESSION)
    await index.index_memory(stored)

    point = raw.upsert_calls[0]["points"][0]
    payload: dict[str, Any] = point.payload
    assert payload == {
        "memory_id": "mem-1",
        "scope": "session",
        "memory_type": "fact",
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "project_id": "proj-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "importance": 0.75,
        "valid_from": None,
        "invalid_at": None,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "content_hash": "sha256:abc",
    }
    assert stored.content not in str(payload)
    assert point.vector == EMBEDDING
    assert embedder.texts == [stored.content]


@pytest.mark.asyncio
async def test_supplied_embedding_bypasses_the_embedder(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
    embedder: RecordingEmbedder,
) -> None:
    await index.index_memory(memory(), embedding=(0.9, 0.8, 0.7))

    assert embedder.texts == []
    assert raw.upsert_calls[0]["points"][0].vector == [0.9, 0.8, 0.7]


@pytest.mark.asyncio
async def test_wrong_sized_embedding_is_rejected_before_any_write(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    with pytest.raises(HarborVectorDimensionError, match="dimension 2 does not match 3"):
        await index.index_memory(memory(), embedding=(0.1, 0.2))

    assert raw.upsert_calls == []
    assert raw.create_collection_calls == []


@pytest.mark.asyncio
async def test_indexing_without_an_embedding_or_embedder_is_a_validation_error(
    repository: QdrantVectorRepository,
) -> None:
    index = QdrantMemoryIndex(repository, dimensions=DIMENSIONS)

    with pytest.raises(HarborStorageValidationError, match="requires an embedding"):
        await index.index_memory(memory())


@pytest.mark.asyncio
async def test_delete_removes_the_point_from_the_owning_tenant(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    await index.delete_memory(owner(tenant_id="tenant-b"), "mem-1")

    call = raw.delete_calls[0]
    assert call["collection_name"] == "tenant-b_memories"
    selector = call["points_selector"].filter
    # The logical memory id is mapped to its stable provider point id.
    assert selector.must[0].has_id == [QdrantMapper.point_id("mem-1")]


@pytest.mark.asyncio
async def test_provider_failures_surface_as_normalized_storage_errors(
    index: QdrantMemoryIndex,
    raw: ExtendedRawQdrant,
) -> None:
    async def explode(**kwargs: Any) -> None:
        del kwargs
        raise RuntimeError("provider exploded")

    raw.upsert = explode  # type: ignore[method-assign]

    with pytest.raises(HarborStorageError, match="memory index index_memory failed") as failure:
        await index.index_memory(memory())

    assert isinstance(failure.value.original, RuntimeError)
    assert failure.value.context.resource_name == MEMORY_INDEX
    assert failure.value.context.tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_repository_validation_errors_pass_through_unwrapped(
    index: QdrantMemoryIndex,
) -> None:
    unsafe = owner(tenant_id="tenant/name")

    with pytest.raises(HarborStorageValidationError, match="Qdrant tenant name"):
        await index.index_memory(memory(owner=unsafe))
