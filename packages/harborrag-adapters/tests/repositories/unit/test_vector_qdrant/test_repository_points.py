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
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    HybridSearchQuery,
    SparseSearchQuery,
    SparseVector,
    VectorIndexRecord,
    VectorIndexSpec,
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
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs", dimension=3
    )
    with pytest.raises(HarborStorageAuthorizationError):
        await repository.upsert_records(
            "docs",
            [VectorIndexRecord(id="p", tenant_id="tenant-b", vector=[1.0, 0.0, 0.0])],
            context=StorageOperationContext.system(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_upsert_keeps_the_approved_payload_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs", dimension=3
    )

    await repository.upsert_records(
        "docs",
        [
            VectorIndexRecord(
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
    assert point.payload == {"k": "v"}


@pytest.mark.asyncio
async def test_upsert_sends_named_dense_and_sparse_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs",
        dimension=3,
        dense_vector_name="dense",
        sparse_vector_name="sparse",
    )

    await repository.upsert_records(
        "docs",
        [
            VectorIndexRecord(
                id="p1",
                tenant_id="tenant-a",
                vector=[1.0, 0.0, 0.0],
                sparse_vector=SparseVector(indices=[2, 7], values=[0.4, 1.2]),
            )
        ],
        context=context,
    )

    [point] = raw.upsert_calls[0]["points"]
    assert point.vector["dense"] == [1.0, 0.0, 0.0]
    assert point.vector["sparse"].indices == [2, 7]
    assert point.vector["sparse"].values == [0.4, 1.2]


@pytest.mark.asyncio
async def test_repository_get_delegates_to_query_executor() -> None:
    raw = ExtendedRawQdrant()
    raw.retrieve_records = [
        SimpleNamespace(
            id="p1",
            payload={},
            vector=[1.0, 0.0, 0.0],
        )
    ]
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext.system(tenant_id="tenant-a")

    points = await repository.get_records("docs", ["point-1"], context=context)

    assert [point.id for point in points] == ["p1"]


@pytest.mark.asyncio
async def test_delete_sends_tenant_scoped_filter_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext.system(tenant_id="tenant-a")

    await repository.delete_records("docs", ["point-1", "point-2"], context=context)

    assert len(raw.delete_calls) == 1
    call = raw.delete_calls[0]
    assert call["collection_name"] == repository._queries.collection_name("docs", context)
    assert call["wait"] is True


@pytest.mark.asyncio
async def test_repository_scan_delegates_to_query_executor() -> None:
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext.system(tenant_id="tenant-a")

    page = await repository.scan_records("docs", limit=10, cursor=None, context=context)

    assert page.records == []
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_repository_search_delegates_to_query_executor() -> None:
    raw = FakeRawQdrant()
    raw.points = [SimpleNamespace(id="point-0", score=0.9, payload={}, vector=None)]
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs", dimension=3
    )
    query = VectorSearchQuery(index_name="docs", vector=[1.0, 0.0, 0.0], top_k=1)

    results = await repository.search(query, context=context)

    assert [result.id for result in results] == ["point-0"]


@pytest.mark.asyncio
async def test_sparse_search_uses_named_sparse_lane_and_normalizes_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(query_module, "qm", ExtendedModels)
    raw = FakeRawQdrant()
    raw.sparse_points = [
        SimpleNamespace(
            id="a",
            score=4.0,
            payload={},
            vector=None,
        )
    ]
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs",
        dimension=3,
        dense_vector_name="dense",
        sparse_vector_name="sparse",
    )

    results = await repository.sparse_search(
        SparseSearchQuery(
            index_name="docs",
            sparse_vector=SparseVector(indices=[2], values=[1.0]),
            top_k=1,
        ),
        context=context,
    )

    assert [result.id for result in results] == ["a"]
    assert results[0].score == pytest.approx(0.8)
    assert raw.query_calls[0]["using"] == "sparse"


@pytest.mark.asyncio
async def test_hybrid_search_raises_capability_error() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext.system(tenant_id="tenant-a")
    query = HybridSearchQuery(
        index_name="docs",
        vector=[1.0, 0.0, 0.0],
        sparse_vector=SparseVector(indices=[0, 1], values=[0.5, 0.5]),
    )

    with pytest.raises(HarborStorageCapabilityError):
        await repository.hybrid_search(query, context=context)


@pytest.mark.asyncio
async def test_hybrid_search_fuses_dense_and_sparse_rankings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    monkeypatch.setattr(query_module, "qm", ExtendedModels)
    raw = FakeRawQdrant()
    raw.dense_points = [
        SimpleNamespace(
            id="dense-only",
            score=0.9,
            payload={},
            vector=None,
        ),
        SimpleNamespace(
            id="both",
            score=0.8,
            payload={},
            vector=None,
        ),
    ]
    raw.sparse_points = [
        SimpleNamespace(
            id="both",
            score=5.0,
            payload={},
            vector=None,
        ),
        SimpleNamespace(
            id="sparse-only",
            score=4.0,
            payload={},
            vector=None,
        ),
    ]
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs",
        dimension=3,
        dense_vector_name="dense",
        sparse_vector_name="sparse",
    )

    results = await repository.hybrid_search(
        HybridSearchQuery(
            index_name="docs",
            vector=[1.0, 0.0, 0.0],
            sparse_vector=SparseVector(indices=[2, 7], values=[0.4, 1.2]),
            dense_weight=0.5,
            top_k=3,
        ),
        context=context,
    )

    assert [result.id for result in results] == [
        "both",
        "dense-only",
        "sparse-only",
    ]
    assert [call["using"] for call in raw.query_calls] == ["dense", "sparse"]
