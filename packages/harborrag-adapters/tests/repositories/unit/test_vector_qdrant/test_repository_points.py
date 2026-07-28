from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageAuthorizationError,
    HarborStorageCapabilityError,
)
from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    HybridSearchQuery,
    SparseVector,
    VectorCollectionSpec,
    VectorPoint,
    VectorSearchQuery,
)

from .fakes import ExtendedModels, ExtendedRawQdrant, FakeQdrantClient, FakeRawQdrant, make_config


@pytest.mark.asyncio
async def test_qdrant_upsert_rejects_cross_tenant_point_with_storage_error() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )

    with pytest.raises(HarborStorageAuthorizationError):
        await repository.upsert(
            "docs",
            [VectorPoint(id="p", tenant_id="tenant-b", vector=[1.0, 0.0, 0.0])],
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_activate_generation_applies_exact_vector_lifecycle_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")

    await repository.activate_generation(
        "docs",
        artifact_id="artifact-1",
        generation_id="generation-2",
        activate_ids=("new",),
        retire_ids=("changed",),
        delete_ids=("removed",),
        tombstone_ids=("tombstone",),
        context=context,
    )

    assert [call["payload"]["index_state"] for call in raw.set_payload_calls] == [
        "active",
        "retired",
        "tombstoned",
    ]
    assert raw.set_payload_calls[0]["payload"]["is_active"] is True
    assert raw.set_payload_calls[2]["payload"]["tombstone"] is True
    assert len(raw.delete_calls) == 1


@pytest.mark.asyncio
async def test_upsert_sends_points_with_harbor_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )

    await repository.upsert(
        "docs",
        [
            VectorPoint(
                id="p1",
                tenant_id="tenant-a",
                vector=[1.0, 0.0, 0.0],
                payload={"k": "v"},
            )
        ],
        context=context,
    )

    assert len(raw.upsert_calls) == 1
    call = raw.upsert_calls[0]
    assert call["collection_name"] == repository._queries.collection_name("docs", context)
    assert call["wait"] is True
    [point] = call["points"]
    assert point.payload["_harbor_tenant_id"] == "tenant-a"
    assert point.payload["_harbor_point_id"] == "p1"
    assert point.payload["k"] == "v"


@pytest.mark.asyncio
async def test_repository_get_delegates_to_query_executor() -> None:
    raw = ExtendedRawQdrant()
    raw.retrieve_records = [
        SimpleNamespace(
            id="p1",
            payload={"_harbor_tenant_id": "tenant-a", "_harbor_point_id": "point-1"},
            vector=[1.0, 0.0, 0.0],
        )
    ]
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")

    points = await repository.get("docs", ["point-1"], context=context)

    assert [point.id for point in points] == ["point-1"]


@pytest.mark.asyncio
async def test_delete_sends_tenant_scoped_filter_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")

    await repository.delete("docs", ["point-1", "point-2"], context=context)

    assert len(raw.delete_calls) == 1
    call = raw.delete_calls[0]
    assert call["collection_name"] == repository._queries.collection_name("docs", context)
    assert call["wait"] is True


@pytest.mark.asyncio
async def test_repository_scan_delegates_to_query_executor() -> None:
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")

    page = await repository.scan("docs", limit=10, cursor=None, context=context)

    assert page.points == []
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_repository_search_delegates_to_query_executor() -> None:
    raw = FakeRawQdrant()
    raw.points = [
        SimpleNamespace(id="0", score=0.9, payload={"_harbor_point_id": "point-0"}, vector=None)
    ]
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )
    query = VectorSearchQuery(collection="docs", vector=[1.0, 0.0, 0.0], top_k=1)

    results = await repository.search(query, context=context)

    assert [result.id for result in results] == ["point-0"]


@pytest.mark.asyncio
async def test_hybrid_search_raises_capability_error() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    query = HybridSearchQuery(
        collection="docs",
        vector=[1.0, 0.0, 0.0],
        sparse_vector=SparseVector(indices=[0, 1], values=[0.5, 0.5]),
    )

    with pytest.raises(HarborStorageCapabilityError):
        await repository.hybrid_search(query, context=context)
