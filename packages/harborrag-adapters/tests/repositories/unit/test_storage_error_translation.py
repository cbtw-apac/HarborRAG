"""Storage adapters translate vendor errors into HarborRAG storage errors."""

from __future__ import annotations

from typing import Any

import grpc
import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from grpc.aio import AioRpcError, Metadata
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from harborrag_adapters.repositories.errors import (
    HarborStorageConnectionError,
    HarborStorageError,
    HarborStorageRateLimitError,
    HarborStorageTimeoutError,
    HarborStorageValidationError,
)
from harborrag_adapters.repositories.graph.falkordb import client as falkordb_client_module
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorIndexRecord, VectorIndexSpec

from .test_graph_falkordb.fakes import FalkorDBWithGraph, client_kwargs
from .test_object_store_s3.fakes import ExtendedFakeS3Raw, make_extended_store
from .test_vector_qdrant.fakes import FakeQdrantClient, FakeRawQdrant, make_config

pytestmark = pytest.mark.unit

CONTEXT = StorageOperationContext.system(tenant_id="tenant-a")
VENDOR_TEXT = b"demo-payload-000"


def _http(status: int) -> UnexpectedResponse:
    return UnexpectedResponse(status, "reason", VENDOR_TEXT, httpx.Headers())


def _grpc(code: grpc.StatusCode) -> AioRpcError:
    return AioRpcError(code, Metadata(), Metadata(), details="demo-payload-000")


class _DownRawQdrant(FakeRawQdrant):
    """Raw Qdrant client whose every call fails with one error."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def collection_exists(self, name: str) -> bool:
        raise self.error

    async def upsert(self, **kwargs: Any) -> None:
        raise self.error


def _qdrant(error: Exception) -> QdrantVectorRepository:
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(_DownRawQdrant(error)),  # type: ignore[arg-type]
    )
    repository._specs[repository._queries.spec_key("docs", CONTEXT)] = VectorIndexSpec(
        index_name="docs", dimension=3
    )
    return repository


_QDRANT_CASES = [
    (ResponseHandlingException(httpx.ConnectError("refused")), HarborStorageConnectionError, True),
    (ResponseHandlingException(httpx.ReadTimeout("slow")), HarborStorageTimeoutError, True),
    (_http(503), HarborStorageConnectionError, True),
    (_http(429), HarborStorageRateLimitError, True),
    (_http(400), HarborStorageValidationError, False),
    (_http(422), HarborStorageValidationError, False),
    (_grpc(grpc.StatusCode.UNAVAILABLE), HarborStorageConnectionError, True),
    (_grpc(grpc.StatusCode.DEADLINE_EXCEEDED), HarborStorageTimeoutError, True),
    (_grpc(grpc.StatusCode.INVALID_ARGUMENT), HarborStorageValidationError, False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "expected", "retryable"), _QDRANT_CASES)
async def test_qdrant_vendor_error_becomes_a_storage_error(
    error: Exception, expected: type[HarborStorageError], retryable: bool
) -> None:
    repository = _qdrant(error)
    record = VectorIndexRecord(id="p1", tenant_id="tenant-a", vector=[1.0, 0.0, 0.0])

    with pytest.raises(expected) as by_collections:
        await repository.index_exists("docs", context=CONTEXT)
    with pytest.raises(expected) as by_points:
        await repository.upsert_records("docs", [record], context=CONTEXT)

    for caught in (by_collections.value, by_points.value):
        assert caught.context.retryable is retryable
        assert caught.context.backend == "qdrant"
        assert caught.__cause__ is error
        assert "demo-payload-000" not in str(caught)
    assert by_points.value.context.operation == "upsert_records"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_http(409), _grpc(grpc.StatusCode.ALREADY_EXISTS)])
async def test_qdrant_error_without_a_storage_meaning_passes_through(error: Exception) -> None:
    with pytest.raises(type(error)):
        await _qdrant(error).index_exists("docs", context=CONTEXT)


def test_translated_outages_are_unavailable_errors() -> None:
    assert issubclass(HarborStorageConnectionError, HarborUnavailableError)
    assert issubclass(HarborStorageTimeoutError, HarborUnavailableError)


class _DownGraph:
    """FalkorDB graph whose queries fail with one error."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def query(self, statement: str, params: dict[str, Any]) -> None:
        raise self.error

    async def ro_query(self, statement: str, params: dict[str, Any]) -> None:
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RedisConnectionError("refused"), HarborStorageConnectionError),
        (RedisTimeoutError("slow"), HarborStorageTimeoutError),
    ],
)
async def test_falkordb_outage_becomes_a_transient_storage_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[HarborStorageError]
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBWithGraph)
    client = FalkorDBClient(**client_kwargs())
    await client.connect()
    client._graph = _DownGraph(error)

    with pytest.raises(expected) as write:
        await client.write("CREATE (n)", {})
    with pytest.raises(expected) as read:
        await client.read("MATCH (n) RETURN n", {})

    assert (write.value.context.operation, read.value.context.operation) == ("write", "read")
    assert write.value.context.retryable and read.value.context.retryable
    assert write.value.__cause__ is error


@pytest.mark.asyncio
async def test_falkordb_response_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBWithGraph)
    client = FalkorDBClient(**client_kwargs())
    await client.connect()
    client._graph = _DownGraph(ResponseError("Attribute 'node_key' is already indexed"))

    with pytest.raises(ResponseError):
        await client.write("CREATE INDEX FOR (n:Node) ON (n.node_key)", {})


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject"
    )


class _DownS3Raw(ExtendedFakeS3Raw):
    """Raw S3 client whose object reads fail with one error."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected", "retryable"),
    [
        (
            EndpointConnectionError(endpoint_url="http://minio.invalid"),
            HarborStorageConnectionError,
            True,
        ),
        (ReadTimeoutError(endpoint_url="http://minio.invalid"), HarborStorageTimeoutError, True),
        (_client_error("SlowDown", 503), HarborStorageConnectionError, True),
        (_client_error("InvalidArgument", 400), HarborStorageValidationError, False),
    ],
)
async def test_s3_vendor_error_becomes_a_storage_error(
    error: Exception, expected: type[HarborStorageError], retryable: bool
) -> None:
    store = make_extended_store(_DownS3Raw(error))

    with pytest.raises(expected) as caught:
        await store.head("bucket", "docs/a.txt", context=CONTEXT)

    assert caught.value.context.retryable is retryable
    assert caught.value.context.operation == "head"
    assert caught.value.__cause__ is error


@pytest.mark.asyncio
async def test_s3_access_denied_passes_through() -> None:
    error = _client_error("AccessDenied", 403)

    with pytest.raises(ClientError):
        await make_extended_store(_DownS3Raw(error)).head("bucket", "a.txt", context=CONTEXT)
