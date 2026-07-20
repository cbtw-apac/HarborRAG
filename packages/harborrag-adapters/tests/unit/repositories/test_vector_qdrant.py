from __future__ import annotations

from enum import StrEnum
from types import SimpleNamespace
from typing import Any

import pytest
from harborrag_adapters.repositories.errors import (
    HarborStorageAuthorizationError,
    HarborStorageCapabilityError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
    HarborVectorDimensionError,
)
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant import repository as repository_module
from harborrag_adapters.repositories.vector.qdrant.config import (
    QdrantDeployment,
    QdrantVectorConfig,
)
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_adapters.repositories.vector.qdrant.query import QdrantQueryExecutor
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext
from harborrag_core.schemas.vector import (
    FilterOperator,
    HybridSearchQuery,
    SparseVector,
    VectorCollectionSpec,
    VectorDistance,
    VectorFilter,
    VectorFilterCondition,
    VectorPoint,
    VectorSearchQuery,
)
from pydantic import ValidationError


class ModelValue:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class Distance(StrEnum):
    COSINE = "Cosine"
    DOT = "Dot"
    EUCLID = "Euclid"
    MANHATTAN = "Manhattan"


class FakeModels:
    Distance = Distance
    Filter = ModelValue
    FilterSelector = ModelValue
    FieldCondition = ModelValue
    MatchValue = ModelValue


class FakeRawQdrant:
    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []
        self.delete_collection_calls: list[str] = []
        self.query_calls: list[dict[str, Any]] = []
        self.points: list[Any] = []
        self.exists = True

    async def collection_exists(self, name: str) -> bool:
        del name
        return self.exists

    async def get_collection(self, name: str) -> Any:
        del name
        vectors = SimpleNamespace(size=3, distance=Distance.COSINE)
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))

    async def delete(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)

    async def delete_collection(self, *, collection_name: str) -> None:
        self.delete_collection_calls.append(collection_name)

    async def query_points(self, **kwargs: Any) -> Any:
        self.query_calls.append(kwargs)
        offset = kwargs["offset"]
        limit = kwargs["limit"]
        return SimpleNamespace(points=self.points[offset : offset + limit])


class FakeQdrantClient:
    deployment = "remote"
    storage = "remote"
    is_connected = True

    def __init__(self, raw: FakeRawQdrant) -> None:
        self.raw = raw


@pytest.fixture(autouse=True)
def fake_qdrant_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "qm", FakeModels)
    monkeypatch.setattr(query_module, "qm", FakeModels)


def make_config() -> QdrantVectorConfig:
    return QdrantVectorConfig(url="http://qdrant.invalid")


@pytest.mark.asyncio
async def test_delete_collection_removes_only_the_tenant_scoped_physical_collection() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")
    other_context = StorageOperationContext(tenant_id="tenant-b")
    key = repository._queries.spec_key("docs", context)
    other_key = repository._queries.spec_key("docs", other_context)
    repository._specs[key] = VectorCollectionSpec(name="docs", dimension=3)
    repository._specs[other_key] = VectorCollectionSpec(name="docs", dimension=3)

    await repository.delete_collection("docs", context=context)

    assert raw.delete_collection_calls == [repository._queries.collection_name("docs", context)]
    assert key not in repository._specs
    assert other_key in repository._specs


@pytest.mark.asyncio
async def test_collection_spec_is_loaded_from_qdrant_on_cache_miss() -> None:
    raw = FakeRawQdrant()
    specs: dict[tuple[str, str], VectorCollectionSpec] = {}
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs=specs,
    )
    context = StorageOperationContext(tenant_id="tenant-a")

    spec = await executor.require_spec("docs", context)

    assert spec.dimension == 3
    assert spec.distance == VectorDistance.COSINE
    assert specs[executor.spec_key("docs", context)] is spec


@pytest.mark.asyncio
async def test_threshold_search_pages_past_previous_heuristic_cap() -> None:
    raw = FakeRawQdrant()
    raw.points = [
        SimpleNamespace(
            id=str(index),
            score=1.0,
            payload={"_harbor_point_id": f"point-{index}"},
            vector=None,
        )
        for index in range(250)
    ]
    context = StorageOperationContext(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )
    query = VectorSearchQuery(
        collection="docs",
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


# --- Additional coverage: config validation, mapping, query executor, repository ---
# (Everything below is additive; no existing test above was modified.)


class ExtendedModels(FakeModels):
    MatchAny = ModelValue
    IsNullCondition = ModelValue
    PayloadField = ModelValue
    Range = ModelValue
    VectorParams = ModelValue
    PointStruct = ModelValue
    HasIdCondition = ModelValue

    class PayloadSchemaType(StrEnum):
        KEYWORD = "keyword"


class ExtendedRawQdrant(FakeRawQdrant):
    def __init__(self) -> None:
        super().__init__()
        self.existing_dimension = 3
        self.existing_distance: Any = Distance.COSINE
        self.existing_payload_schema: dict[str, Any] = {}
        self.named_vectors = False
        self.create_collection_calls: list[dict[str, Any]] = []
        self.create_payload_index_calls: list[dict[str, Any]] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.retrieve_records: list[Any] = []
        self.scroll_records: list[Any] = []
        self.scroll_next_offset: Any = None

    async def get_collection(self, name: str) -> Any:
        del name
        if self.named_vectors:
            vectors: Any = {
                "dense": SimpleNamespace(
                    size=self.existing_dimension, distance=self.existing_distance
                )
            }
        else:
            vectors = SimpleNamespace(size=self.existing_dimension, distance=self.existing_distance)
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)),
            payload_schema=self.existing_payload_schema,
        )

    async def create_collection(self, **kwargs: Any) -> None:
        self.create_collection_calls.append(kwargs)

    async def create_payload_index(self, **kwargs: Any) -> None:
        self.create_payload_index_calls.append(kwargs)

    async def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)

    async def retrieve(self, **kwargs: Any) -> list[Any]:
        del kwargs
        return self.retrieve_records

    async def scroll(self, **kwargs: Any) -> tuple[list[Any], Any]:
        del kwargs
        return self.scroll_records, self.scroll_next_offset


class ExtendedQdrantClient(FakeQdrantClient):
    def __init__(
        self,
        raw: FakeRawQdrant,
        *,
        is_connected: bool = True,
        ping_error: Exception | None = None,
    ) -> None:
        super().__init__(raw)
        self.is_connected = is_connected
        self._ping_error = ping_error
        self.connect_calls = 0
        self.close_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self.is_connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self.is_connected = False

    async def ping(self) -> None:
        if self._ping_error is not None:
            raise self._ping_error


class _Condition:
    def __init__(self, *, field: str, operator: Any, value: Any = None) -> None:
        self.field = field
        self.operator = operator
        self.value = value


# --- config.py ---


def test_remote_config_requires_url() -> None:
    with pytest.raises(ValidationError, match="requires url"):
        QdrantVectorConfig(deployment=QdrantDeployment.REMOTE)


def test_remote_config_rejects_path() -> None:
    with pytest.raises(ValidationError, match="does not accept path"):
        QdrantVectorConfig(url="http://qdrant.invalid", path="/data")


def test_embedded_config_rejects_url() -> None:
    with pytest.raises(ValidationError, match="does not accept url"):
        QdrantVectorConfig(deployment=QdrantDeployment.EMBEDDED, url="http://qdrant.invalid")


def test_embedded_config_rejects_api_key() -> None:
    with pytest.raises(ValidationError, match="does not accept api_key"):
        QdrantVectorConfig(deployment=QdrantDeployment.EMBEDDED, api_key="secret")


def test_embedded_config_with_no_url_or_api_key_succeeds() -> None:
    config = QdrantVectorConfig(deployment=QdrantDeployment.EMBEDDED)
    assert config.deployment == QdrantDeployment.EMBEDDED
    assert config.url is None


# --- mapping.py ---


def test_distance_and_vector_distance_round_trip_all_metrics() -> None:
    for distance in VectorDistance:
        provider_value = QdrantMapper.distance(distance, FakeModels)
        assert QdrantMapper.vector_distance(provider_value, FakeModels) == distance


def test_point_id_is_stable_and_namespaced_per_tenant() -> None:
    first = QdrantMapper.point_id("tenant-a", "doc-1")
    second = QdrantMapper.point_id("tenant-a", "doc-1")
    other_tenant = QdrantMapper.point_id("tenant-b", "doc-1")
    assert first == second
    assert first != other_tenant


def test_filter_without_value_only_scopes_tenant() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")
    result = QdrantMapper.filter(None, context, ExtendedModels)
    assert len(result.must) == 1
    assert result.must[0].key == "_harbor_tenant_id"
    assert result.must[0].match.value == "tenant-a"


def test_filter_combines_must_should_and_must_not() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")
    filter_value = VectorFilter(
        must=[VectorFilterCondition(field="a", operator=FilterOperator.EQUALS, value=1)],
        should=[VectorFilterCondition(field="b", operator=FilterOperator.EQUALS, value=2)],
        must_not=[VectorFilterCondition(field="c", operator=FilterOperator.EQUALS, value=3)],
    )

    result = QdrantMapper.filter(filter_value, context, ExtendedModels)

    assert len(result.must) == 2  # tenant scope + explicit "must" condition
    assert len(result.should) == 1
    assert len(result.must_not) == 1


def test_condition_covers_every_supported_filter_operator() -> None:
    equals = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.EQUALS, value=1), ExtendedModels
    )
    assert equals.match.value == 1

    in_condition = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.IN, value=[1, 2]), ExtendedModels
    )
    assert in_condition.match.any == [1, 2]

    exists_true = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.EXISTS, value=True), ExtendedModels
    )
    assert exists_true.must_not[0].is_null.key == "a"

    exists_false = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.EXISTS, value=False), ExtendedModels
    )
    assert exists_false.is_null.key == "a"

    greater_than = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.GREATER_THAN, value=5), ExtendedModels
    )
    assert greater_than.range.gt == 5

    greater_or_equal = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.GREATER_THAN_OR_EQUAL, value=5),
        ExtendedModels,
    )
    assert greater_or_equal.range.gte == 5

    less_than = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.LESS_THAN, value=5), ExtendedModels
    )
    assert less_than.range.lt == 5

    less_or_equal = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.LESS_THAN_OR_EQUAL, value=5), ExtendedModels
    )
    assert less_or_equal.range.lte == 5

    not_equals = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.NOT_EQUALS, value=1), ExtendedModels
    )
    assert not_equals.must_not[0].match.value == 1

    not_in = QdrantMapper.condition(
        _Condition(field="a", operator=FilterOperator.NOT_IN, value=[1, 2]), ExtendedModels
    )
    assert not_in.must_not[0].match.any == [1, 2]

    with pytest.raises(ValueError, match="unsupported Qdrant filter operator"):
        QdrantMapper.condition(_Condition(field="a", operator="unsupported"), ExtendedModels)


def test_normalize_score_covers_every_distance_branch() -> None:
    assert QdrantMapper.normalize_score(1.0, VectorDistance.COSINE) == 1.0
    assert QdrantMapper.normalize_score(-3.0, VectorDistance.COSINE) == 0.0
    assert 0.9 < QdrantMapper.normalize_score(5.0, VectorDistance.DOT_PRODUCT) < 1.0
    assert 0.0 < QdrantMapper.normalize_score(-5.0, VectorDistance.DOT_PRODUCT) < 0.1
    assert QdrantMapper.normalize_score(0.0, VectorDistance.EUCLIDEAN) == 1.0
    assert 0.0 < QdrantMapper.normalize_score(1.0, VectorDistance.MANHATTAN) < 1.0


# --- query.py ---


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
async def test_get_filters_cross_tenant_records_and_defaults_missing_logical_id() -> None:
    raw = ExtendedRawQdrant()
    context = StorageOperationContext(tenant_id="tenant-a")
    raw.retrieve_records = [
        SimpleNamespace(
            id="p1",
            payload={"_harbor_tenant_id": "tenant-a", "_harbor_point_id": "point-1", "field": "x"},
            vector=[1.0, 0.0, 0.0],
        ),
        SimpleNamespace(
            id="p2",
            payload={"_harbor_tenant_id": "tenant-b", "_harbor_point_id": "point-2"},
            vector=[1.0, 0.0, 0.0],
        ),
        SimpleNamespace(
            id="p3",
            payload={"_harbor_tenant_id": "tenant-a"},
            vector=[0.0, 0.0, 0.0],
        ),
    ]
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    points = await executor.get("docs", ["point-1", "point-3"], context=context)

    assert [point.id for point in points] == ["point-1", "p3"]
    assert points[0].payload == {"field": "x"}
    assert points[1].vector == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_scan_returns_page_with_stringified_next_cursor() -> None:
    raw = ExtendedRawQdrant()
    context = StorageOperationContext(tenant_id="tenant-a")
    raw.scroll_records = [
        SimpleNamespace(
            id="p1",
            payload={"_harbor_tenant_id": "tenant-a", "_harbor_point_id": "point-1"},
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

    assert [point.id for point in page.points] == ["point-1"]
    assert page.next_cursor == "cursor-2"


@pytest.mark.asyncio
async def test_scan_returns_none_cursor_when_provider_is_exhausted() -> None:
    raw = ExtendedRawQdrant()
    context = StorageOperationContext(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    page = await executor.scan("docs", limit=10, cursor=None, context=context)

    assert page.points == []
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_search_without_threshold_uses_a_single_query_points_call() -> None:
    raw = FakeRawQdrant()
    raw.points = [
        SimpleNamespace(
            id=str(i), score=0.9, payload={"_harbor_point_id": f"point-{i}"}, vector=None
        )
        for i in range(3)
    ]
    context = StorageOperationContext(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )
    query = VectorSearchQuery(collection="docs", vector=[1.0, 0.0, 0.0], top_k=3)

    results = await executor.search(query, context=context)

    assert [result.id for result in results] == ["point-0", "point-1", "point-2"]
    assert len(raw.query_calls) == 1
    assert raw.query_calls[0]["limit"] == 3
    assert raw.query_calls[0]["offset"] == 0


@pytest.mark.asyncio
async def test_threshold_search_stops_when_provider_returns_no_points() -> None:
    raw = FakeRawQdrant()
    raw.points = []
    context = StorageOperationContext(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )
    query = VectorSearchQuery(
        collection="docs", vector=[1.0, 0.0, 0.0], top_k=5, score_threshold=0.5
    )

    results = await executor.search(query, context=context)

    assert results == []
    assert len(raw.query_calls) == 1


@pytest.mark.asyncio
async def test_threshold_search_skips_results_below_threshold() -> None:
    raw = FakeRawQdrant()
    raw.points = [
        SimpleNamespace(id="0", score=1.0, payload={"_harbor_point_id": "high"}, vector=None),
        SimpleNamespace(id="1", score=-1.0, payload={"_harbor_point_id": "low"}, vector=None),
    ]
    context = StorageOperationContext(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    executor._specs[executor.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3, distance=VectorDistance.COSINE
    )
    query = VectorSearchQuery(
        collection="docs", vector=[1.0, 0.0, 0.0], top_k=5, score_threshold=0.9
    )

    results = await executor.search(query, context=context)

    assert [result.id for result in results] == ["high"]


@pytest.mark.asyncio
async def test_require_spec_raises_not_found_when_collection_missing() -> None:
    raw = FakeRawQdrant()
    raw.exists = False
    context = StorageOperationContext(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    with pytest.raises(HarborStorageNotFoundError):
        await executor.require_spec("docs", context)


@pytest.mark.asyncio
async def test_require_spec_rejects_named_vector_collections() -> None:
    raw = ExtendedRawQdrant()
    raw.named_vectors = True
    context = StorageOperationContext(tenant_id="tenant-a")
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )

    with pytest.raises(HarborStorageCapabilityError):
        await executor.require_spec("docs", context)


def test_assert_dimension_rejects_mismatched_vector_length() -> None:
    raw = FakeRawQdrant()
    executor = QdrantQueryExecutor(
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
        config=make_config(),
        specs={},
    )
    spec = VectorCollectionSpec(name="docs", dimension=3)

    with pytest.raises(HarborVectorDimensionError):
        executor.assert_dimension(spec, [1.0, 0.0])


# --- repository.py ---


def test_repository_requires_qm_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "qm", None)
    with pytest.raises(ImportError):
        QdrantVectorRepository(make_config())


def test_capabilities_reports_dense_only_baseline() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]

    capabilities = repository.capabilities

    assert capabilities.dense_vectors is True
    assert capabilities.hybrid_search is False
    assert capabilities.sparse_vectors is False


@pytest.mark.asyncio
async def test_connect_and_close_delegate_to_database_client() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw)
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    await repository.connect()
    assert client.connect_calls == 1

    await repository.close()
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_health_reports_unknown_when_not_connected() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw, is_connected=False)
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    health = await repository.health()

    assert health.status == HealthStatus.UNKNOWN
    assert health.details["deployment"] == client.deployment


@pytest.mark.asyncio
async def test_health_reports_healthy_when_ping_succeeds() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw, is_connected=True)
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    health = await repository.health()

    assert health.status == HealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_health_reports_unhealthy_when_ping_raises() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw, is_connected=True, ping_error=RuntimeError("down"))
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    health = await repository.health()

    assert health.status == HealthStatus.UNHEALTHY
    assert health.details["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_ensure_collection_rejects_non_tenant_scoped_spec() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, tenant_scoped=False)

    with pytest.raises(HarborStorageValidationError):
        await repository.ensure_collection(spec, context=context)


@pytest.mark.asyncio
async def test_ensure_collection_creates_new_collection_with_metadata_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    raw.exists = False
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, metadata_indexes=["source", "kind"])

    await repository.ensure_collection(spec, context=context)

    assert raw.create_collection_calls[0]["collection_name"] == repository._queries.collection_name(
        "docs", context
    )
    assert len(raw.create_payload_index_calls) == 2
    assert repository._specs[repository._queries.spec_key("docs", context)] is spec


@pytest.mark.asyncio
async def test_ensure_collection_matches_existing_and_only_adds_missing_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    raw.exists = True
    raw.existing_dimension = 3
    raw.existing_distance = Distance.COSINE
    raw.existing_payload_schema = {"source": object()}
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, metadata_indexes=["source", "kind"])

    await repository.ensure_collection(spec, context=context)

    assert raw.create_collection_calls == []
    assert len(raw.create_payload_index_calls) == 1
    assert raw.create_payload_index_calls[0]["field_name"] == "kind"
    assert repository._specs[repository._queries.spec_key("docs", context)] is spec


@pytest.mark.asyncio
async def test_ensure_collection_raises_when_existing_dimension_mismatches() -> None:
    raw = ExtendedRawQdrant()
    raw.exists = True
    raw.existing_dimension = 5
    raw.existing_distance = Distance.COSINE
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3)

    with pytest.raises(HarborStorageValidationError):
        await repository.ensure_collection(spec, context=context)


@pytest.mark.asyncio
async def test_collection_exists_delegates_to_database_client() -> None:
    raw = FakeRawQdrant()
    raw.exists = True
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")

    assert await repository.collection_exists("docs", context=context) is True


@pytest.mark.asyncio
async def test_delete_collection_when_absent_only_clears_cache() -> None:
    raw = FakeRawQdrant()
    raw.exists = False
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )

    await repository.delete_collection("docs", context=context)

    assert raw.delete_collection_calls == []
    assert repository._queries.spec_key("docs", context) not in repository._specs


@pytest.mark.asyncio
async def test_upsert_sends_points_with_harbor_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )

    await repository.upsert(
        "docs",
        [VectorPoint(id="p1", tenant_id="tenant-a", vector=[1.0, 0.0, 0.0], payload={"k": "v"})],
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
async def test_delete_sends_tenant_scoped_filter_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
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
