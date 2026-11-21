"""A storage outage the adapters translated still answers 503 dependency_unavailable."""

from __future__ import annotations

from typing import Any

import grpc
import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from grpc.aio import AioRpcError, Metadata
from qdrant_client.http.exceptions import ResponseHandlingException
from redis.exceptions import ConnectionError as RedisConnectionError

from harborrag_adapters.repositories.graph.falkordb import client as falkordb_client_module
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.repository import S3ObjectStore
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_app.operations import error_body
from harborrag_core.storage import StorageOperationContext

pytestmark = pytest.mark.unit

CONTEXT = StorageOperationContext.system(tenant_id="acme")


class _Down:
    """A vendor client whose every call fails with one error."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __getattr__(self, name: str) -> Any:
        async def fail(*args: Any, **kwargs: Any) -> Any:
            raise self.error

        return fail


class _Holder:
    """Database client wrapper exposing the vendor client as ``raw``."""

    def __init__(self, raw: _Down) -> None:
        self.raw = raw
        self.graph = raw


async def _qdrant_outage(error: Exception) -> BaseException:
    repository = QdrantVectorRepository(
        QdrantVectorConfig(url="http://qdrant.invalid", allow_insecure_remote=True),
        client=_Holder(_Down(error)),  # type: ignore[arg-type]
    )
    with pytest.raises(Exception) as caught:
        await repository.index_exists("docs", context=CONTEXT)
    return caught.value


async def _s3_outage(error: Exception) -> BaseException:
    store = S3ObjectStore(S3ObjectStoreConfig(), client=_Holder(_Down(error)))  # type: ignore[arg-type]
    with pytest.raises(Exception) as caught:
        await store.head("bucket", "docs/a.txt", context=CONTEXT)
    return caught.value


async def _falkordb_outage(error: Exception, monkeypatch: pytest.MonkeyPatch) -> BaseException:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", lambda **_: _Holder(_Down(error)))
    client = FalkorDBClient(
        host="falkordb.invalid",
        port=6379,
        username=None,
        password=None,
        graph_name="harborrag",
        ssl=False,
        max_connections=1,
        connect_timeout_seconds=1,
        operation_timeout_seconds=1,
    )
    client._database = _Holder(_Down(error))
    client._graph = _Down(error)
    with pytest.raises(Exception) as caught:
        await client.read("MATCH (n) RETURN n", {})
    return caught.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AioRpcError(grpc.StatusCode.UNAVAILABLE, Metadata(), Metadata(), details="refused"),
        ResponseHandlingException(httpx.ConnectError("refused")),
    ],
)
async def test_error_body_for_a_qdrant_outage_is_dependency_unavailable(error: Exception) -> None:
    body = error_body(await _qdrant_outage(error))

    assert (body.status, body.code) == (503, "dependency_unavailable")


@pytest.mark.asyncio
async def test_error_body_for_an_object_store_outage_is_dependency_unavailable() -> None:
    outage = await _s3_outage(EndpointConnectionError(endpoint_url="http://minio.invalid"))

    body = error_body(outage)

    assert (body.status, body.code) == (503, "dependency_unavailable")


@pytest.mark.asyncio
async def test_error_body_for_a_graph_outage_is_dependency_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = error_body(await _falkordb_outage(RedisConnectionError("refused"), monkeypatch))

    assert (body.status, body.code) == (503, "dependency_unavailable")
