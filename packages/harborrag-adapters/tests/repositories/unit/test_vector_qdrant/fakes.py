from __future__ import annotations

from enum import StrEnum
from types import SimpleNamespace
from typing import Any

from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig


class ModelValue:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class Distance(StrEnum):
    COSINE = "Cosine"
    DOT = "Dot"
    EUCLID = "Euclid"
    MANHATTAN = "Manhattan"


class Modifier(StrEnum):
    IDF = "idf"


class FakeModels:
    Distance = Distance
    Modifier = Modifier
    Filter = ModelValue
    FilterSelector = ModelValue
    FieldCondition = ModelValue
    MatchValue = ModelValue
    SparseVector = ModelValue


class FakeRawQdrant:
    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []
        self.delete_collection_calls: list[str] = []
        self.query_calls: list[dict[str, Any]] = []
        self.points: list[Any] = []
        self.dense_points: list[Any] | None = None
        self.sparse_points: list[Any] | None = None
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
        offset = kwargs.get("offset", 0)
        limit = kwargs["limit"]
        selected = self.points
        if kwargs.get("using") == "dense" and self.dense_points is not None:
            selected = self.dense_points
        if kwargs.get("using") == "sparse" and self.sparse_points is not None:
            selected = self.sparse_points
        return SimpleNamespace(points=selected[offset : offset + limit])


class FakeQdrantClient:
    deployment = "remote"
    storage = "remote"
    is_connected = True

    def __init__(self, raw: FakeRawQdrant) -> None:
        self.raw = raw


class ExtendedModels(FakeModels):
    MatchAny = ModelValue
    IsNullCondition = ModelValue
    PayloadField = ModelValue
    Range = ModelValue
    VectorParams = ModelValue
    SparseVectorParams = ModelValue
    Modifier = Modifier
    PointStruct = ModelValue
    HasIdCondition = ModelValue

    class PayloadSchemaType(StrEnum):
        BOOL = "bool"
        KEYWORD = "keyword"


class ExtendedRawQdrant(FakeRawQdrant):
    def __init__(self) -> None:
        super().__init__()
        self.existing_dimension = 3
        self.existing_distance: Any = Distance.COSINE
        self.existing_payload_schema: dict[str, Any] = {}
        self.named_vectors = False
        self.sparse_vectors = False
        self.create_collection_calls: list[dict[str, Any]] = []
        self.create_payload_index_calls: list[dict[str, Any]] = []
        self.delete_payload_index_calls: list[dict[str, Any]] = []
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
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=vectors,
                    sparse_vectors=(
                        {"sparse": SimpleNamespace(modifier=Modifier.IDF)}
                        if self.sparse_vectors
                        else {}
                    ),
                )
            ),
            payload_schema=self.existing_payload_schema,
        )

    async def create_collection(self, **kwargs: Any) -> None:
        self.create_collection_calls.append(kwargs)

    async def create_payload_index(self, **kwargs: Any) -> None:
        self.create_payload_index_calls.append(kwargs)

    async def delete_payload_index(self, **kwargs: Any) -> None:
        self.delete_payload_index_calls.append(kwargs)

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


class Condition:
    def __init__(self, *, field: str, operator: Any, value: Any = None) -> None:
        self.field = field
        self.operator = operator
        self.value = value


def make_config() -> QdrantVectorConfig:
    return QdrantVectorConfig(
        url="http://qdrant.invalid",
        allow_insecure_remote=True,
    )
