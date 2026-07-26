from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.vector.qdrant import client as qdrant_client_module
from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

from .fakes import FakeAsyncQdrantClient


def test_qdrant_client_requires_async_client_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", None)

    with pytest.raises(ImportError):
        QdrantDBClient(
            deployment=QdrantDeployment.EMBEDDED,
            url=None,
            path=None,
            api_key=None,
            prefer_grpc=False,
            operation_timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_qdrant_client_embedded_disk_connect_passes_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.EMBEDDED,
        url=None,
        path="/tmp/qdrant-data",
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    await client.connect()

    assert client.backend == "qdrant"
    assert client.storage == "disk"
    assert client.is_connected is True
    assert client.raw.kwargs == {"path": "/tmp/qdrant-data"}
    assert client.raw.get_collections_calls == 1

    await client.close()

    assert client.is_connected is False


@pytest.mark.asyncio
async def test_qdrant_client_embedded_memory_connect_uses_memory_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.EMBEDDED,
        url=None,
        path=None,
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    await client.connect()

    assert client.storage == "memory"
    assert client.raw.kwargs == {"location": ":memory:"}


@pytest.mark.asyncio
async def test_qdrant_client_remote_connect_passes_url_and_rounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.REMOTE,
        url="http://qdrant.invalid",
        path=None,
        api_key="secret",
        prefer_grpc=True,
        operation_timeout_seconds=2.4,
    )

    await client.connect()

    assert client.storage == "remote"
    assert client.raw.kwargs == {
        "url": "http://qdrant.invalid",
        "api_key": "secret",
        "prefer_grpc": True,
        "timeout": 3,
    }


@pytest.mark.asyncio
async def test_qdrant_client_connect_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingAsyncQdrantClient:
        instances = 0

        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            CountingAsyncQdrantClient.instances += 1

        async def get_collections(self) -> list[str]:
            return []

        async def close(self) -> None:
            pass

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", CountingAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.EMBEDDED,
        url=None,
        path=None,
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    await client.connect()
    await client.connect()

    assert CountingAsyncQdrantClient.instances == 1


@pytest.mark.asyncio
async def test_qdrant_client_connect_failure_closes_underlying_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingAsyncQdrantClient:
        last_instance: FailingAsyncQdrantClient | None = None

        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.closed = False
            FailingAsyncQdrantClient.last_instance = self

        async def get_collections(self) -> list[str]:
            raise RuntimeError("boom")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FailingAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.EMBEDDED,
        url=None,
        path=None,
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    with pytest.raises(RuntimeError):
        await client.connect()

    assert client.is_connected is False
    assert FailingAsyncQdrantClient.last_instance is not None
    assert FailingAsyncQdrantClient.last_instance.closed is True


def test_qdrant_client_raw_property_raises_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.EMBEDDED,
        url=None,
        path=None,
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    with pytest.raises(RuntimeError):
        _ = client.raw


@pytest.mark.asyncio
async def test_qdrant_client_close_without_connect_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.EMBEDDED,
        url=None,
        path=None,
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    await client.close()

    assert client.is_connected is False


def test_qdrant_client_deployment_property_reports_configured_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.REMOTE,
        url="http://qdrant.invalid",
        path=None,
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    assert client.deployment == QdrantDeployment.REMOTE
