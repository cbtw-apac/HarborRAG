from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    client as falkordb_client_module,
)
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.vector.client import HarborVectorDBClient
from harborrag_adapters.repositories.vector.qdrant import client as qdrant_client_module
from harborrag_adapters.repositories.vector.qdrant import query as qdrant_query_module
from harborrag_adapters.repositories.vector.qdrant import repository as qdrant_repository_module
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.repository import (
    QdrantVectorRepository,
)

from .fakes import FakeAsyncQdrantClient, FalkorDBWithoutDirectClose


def test_default_clients_register_only_supported_backends() -> None:
    assert HarborVectorDBClient.default().backends() == ("qdrant",)


@pytest.mark.asyncio
async def test_falkordb_client_closes_sdk_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        falkordb_client_module,
        "FalkorDB",
        FalkorDBWithoutDirectClose,
    )
    client = FalkorDBClient(
        host="localhost",
        port=6379,
        username=None,
        password=None,
        graph_name="smoke",
        ssl=False,
        max_connections=1,
        connect_timeout_seconds=1,
        operation_timeout_seconds=1,
    )

    await client.connect()
    connection = client.raw.connection
    await client.close()

    assert connection.closed is True


def test_vector_client_capabilities_create_and_create_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    monkeypatch.setattr(qdrant_repository_module, "qm", object())
    monkeypatch.setattr(qdrant_query_module, "qm", object())
    client = HarborVectorDBClient.default()
    assert client.capabilities("qdrant") is None

    created = client.create(
        backend="qdrant",
        options={"deployment": "embedded", "path": "/tmp/qdrant-client-test"},
    )
    assert isinstance(created, QdrantVectorRepository)

    from_config = client.create_from_config(
        QdrantVectorConfig(deployment="embedded", path="/tmp/qdrant-client-test")
    )
    assert isinstance(from_config, QdrantVectorRepository)
