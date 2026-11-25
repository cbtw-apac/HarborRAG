from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.repositories.vector.qdrant import client as qdrant_client_module
from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.config import (
    QdrantDeployment,
    QdrantVectorConfig,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository

from .fakes import FakeAsyncQdrantClient

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_qdrant_client_remote_connect_passes_an_explicit_grpc_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.REMOTE,
        url="http://localhost:16333",
        path=None,
        api_key=None,
        prefer_grpc=True,
        grpc_port=16334,
        operation_timeout_seconds=5,
    )

    await client.connect()

    assert client.raw.kwargs["grpc_port"] == 16334


@pytest.mark.asyncio
async def test_qdrant_repository_hands_the_configured_grpc_port_to_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    repository = QdrantVectorRepository(
        QdrantVectorConfig(url="http://localhost:16333", grpc_port=16334)
    )

    await repository.connect()

    assert repository._database.raw.kwargs["grpc_port"] == 16334


@pytest.mark.parametrize("port", [0, 65_536])
def test_qdrant_config_refuses_a_grpc_port_outside_the_tcp_range(port: int) -> None:
    with pytest.raises(ValidationError):
        QdrantVectorConfig(url="http://localhost:6333", grpc_port=port)
