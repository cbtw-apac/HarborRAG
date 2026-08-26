from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageNotFoundError,
    HarborStorageValidationError,
    HarborVectorDimensionError,
)
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant.query import QdrantQueryExecutor
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorDistance, VectorIndexSpec, VectorSearchQuery

from .fakes import ExtendedRawQdrant, FakeQdrantClient, FakeRawQdrant, make_config


def test_collection_name_is_readable_and_tenant_scoped() -> None:
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(FakeRawQdrant()),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    assert (
        executor.collection_name(
            "routes",
            StorageOperationContext.system(tenant_id="DEFAULT"),
        )
        == "DEFAULT_routes"
    )
    assert (
        executor.collection_name(
            "evidence",
            StorageOperationContext.system(tenant_id="tenant-a"),
        )
        == "tenant-a_evidence"
    )


def test_collection_name_rejects_unsafe_tenant_identifiers() -> None:
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(FakeRawQdrant()),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    with pytest.raises(HarborStorageValidationError, match="Qdrant tenant name"):
        executor.collection_name(
            "routes",
            StorageOperationContext.system(tenant_id="tenant/name"),
        )


@pytest.mark.asyncio
async def test_collection_spec_is_loaded_from_qdrant_on_cache_miss() -> None:
    raw = FakeRawQdrant()
    specs: dict[tuple[str, str], VectorIndexSpec] = {}
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs=specs,
    )
    context = StorageOperationContext.system(tenant_id="tenant-a")

    spec = await executor.require_spec("docs", context)

    assert spec.dimension == 3
    assert spec.distance == VectorDistance.COSINE
    assert specs[executor.spec_key("docs", context)] is spec


@pytest.mark.asyncio
async def test_threshold_search_pages_past_previous_heuristic_cap() -> None:
    raw = FakeRawQdrant()
    raw.points = [
        SimpleNamespace(
            id=f"point-{index}",
            score=1.0,
            payload={},
            vector=None,
        )
        for index in range(250)
    ]
    context = StorageOperationContext.system(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs", dimension=3
    )
    query = VectorSearchQuery(
        index_name="docs",
        vector=[1.0, 0.0, 0.0],
        top_k=5,
        offset=150,
        score_threshold=0.9,
    )

    results = await executor.search(query, context=context)

    assert [result.id for result in results] == [
        "point-150",
        "point-151",
        "point-152",
        "point-153",
        "point-154",
    ]
    assert [call["offset"] for call in raw.query_calls] == [0, 100]


def test_query_executor_requires_qm_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = FakeRawQdrant()
    monkeypatch.setattr(query_module, "qm", None)
    with pytest.raises(ImportError):
        QdrantQueryExecutor(
            client=FakeQdrantClient(raw),  # type: ignore[arg-type]
            config=make_config(),
            specs={},
        )


@pytest.mark.asyncio
async def test_get_uses_provider_ids_inside_the_tenant_collection() -> None:
    raw = ExtendedRawQdrant()
    context = StorageOperationContext.system(tenant_id="tenant-a")
    raw.retrieve_records = [
        SimpleNamespace(
            id="p1",
            payload={"field": "x"},
            vector=[1.0, 0.0, 0.0],
        ),
        SimpleNamespace(
            id="p3",
            payload={},
            vector=[0.0, 0.0, 0.0],
        ),
    ]
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    points = await executor.get("docs", ["point-1", "point-3"], context=context)

    assert [point.id for point in points] == ["p1", "p3"]
    assert points[0].payload == {"field": "x"}
    assert points[1].vector == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_scan_returns_page_with_stringified_next_cursor() -> None:
    raw = ExtendedRawQdrant()
    context = StorageOperationContext.system(tenant_id="tenant-a")
    raw.scroll_records = [
        SimpleNamespace(
            id="p1",
            payload={},
            vector=[1.0, 0.0, 0.0],
        )
    ]
    raw.scroll_next_offset = "cursor-2"
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    page = await executor.scan("docs", limit=10, cursor=None, context=context)

    assert [point.id for point in page.records] == ["p1"]
    assert page.next_cursor == "cursor-2"


@pytest.mark.asyncio
async def test_scan_returns_none_cursor_when_provider_is_exhausted() -> None:
    raw = ExtendedRawQdrant()
    context = StorageOperationContext.system(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    page = await executor.scan("docs", limit=10, cursor=None, context=context)

    assert page.records == []
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_search_without_threshold_uses_a_single_query_points_call() -> None:
    raw = FakeRawQdrant()
    raw.points = [
        SimpleNamespace(
            id=f"point-{i}",
            score=0.9,
            payload={},
            vector=None,
        )
        for i in range(3)
    ]
    context = StorageOperationContext.system(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs", dimension=3
    )
    query = VectorSearchQuery(index_name="docs", vector=[1.0, 0.0, 0.0], top_k=3)

    results = await executor.search(query, context=context)

    assert [result.id for result in results] == ["point-0", "point-1", "point-2"]
    assert len(raw.query_calls) == 1
    assert raw.query_calls[0]["limit"] == 3
    assert raw.query_calls[0]["offset"] == 0


@pytest.mark.asyncio
async def test_threshold_search_stops_when_provider_returns_no_points() -> None:
    raw = FakeRawQdrant()
    raw.points = []
    context = StorageOperationContext.system(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs", dimension=3
    )
    query = VectorSearchQuery(
        index_name="docs", vector=[1.0, 0.0, 0.0], top_k=5, score_threshold=0.5
    )

    results = await executor.search(query, context=context)

    assert results == []
    assert len(raw.query_calls) == 1


@pytest.mark.asyncio
async def test_threshold_search_skips_results_below_threshold() -> None:
    raw = FakeRawQdrant()
    raw.points = [
        SimpleNamespace(id="high", score=1.0, payload={}, vector=None),
        SimpleNamespace(id="low", score=-1.0, payload={}, vector=None),
    ]
    context = StorageOperationContext.system(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs", dimension=3, distance=VectorDistance.COSINE
    )
    query = VectorSearchQuery(
        index_name="docs", vector=[1.0, 0.0, 0.0], top_k=5, score_threshold=0.9
    )

    results = await executor.search(query, context=context)

    assert [result.id for result in results] == ["high"]


@pytest.mark.asyncio
async def test_require_spec_raises_not_found_when_collection_missing() -> None:
    raw = FakeRawQdrant()
    raw.exists = False
    context = StorageOperationContext.system(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    with pytest.raises(HarborStorageNotFoundError):
        await executor.require_spec("docs", context)


@pytest.mark.asyncio
async def test_require_spec_loads_named_dense_and_sparse_collection() -> None:
    raw = ExtendedRawQdrant()
    raw.named_vectors = True
    raw.sparse_vectors = True
    context = StorageOperationContext.system(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    spec = await executor.require_spec("docs", context)

    assert spec.dense_vector_name == "dense"
    assert spec.sparse_vector_name == "sparse"
    assert spec.sparse_idf is True


def test_assert_dimension_rejects_mismatched_vector_length() -> None:
    raw = FakeRawQdrant()
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    spec = VectorIndexSpec(index_name="docs", dimension=3)

    with pytest.raises(HarborVectorDimensionError):
        executor.assert_dimension(spec, [1.0, 0.0])
