from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.cache.client import HarborCacheDBClient
from harborrag_adapters.repositories.cache.memory.config import MemoryCacheConfig
from harborrag_adapters.repositories.cache.memory.repository import MemoryCacheBackend
from harborrag_adapters.repositories.database.client import HarborDatabaseClient
from harborrag_adapters.repositories.database.sqlite.config import SQLiteDatabaseConfig
from harborrag_adapters.repositories.database.sqlite.repository import (
    SQLiteDatabaseBackend,
)
from harborrag_adapters.repositories.graph.client import HarborGraphDBClient
from harborrag_adapters.repositories.graph.falkordb import (
    client as falkordb_client_module,
)
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.repository import (
    FalkorDBGraphRepository,
)
from harborrag_adapters.repositories.object_store.client import (
    HarborObjectStoreDBClient,
)
from harborrag_adapters.repositories.object_store.memory.config import (
    MemoryObjectStoreConfig,
)
from harborrag_adapters.repositories.object_store.memory.repository import (
    MemoryObjectStore,
)
from harborrag_adapters.repositories.state.client import HarborStateDBClient
from harborrag_adapters.repositories.state.sqlite.config import SQLiteStateConfig
from harborrag_adapters.repositories.state.sqlite.repository import SQLiteStateBackend
from harborrag_adapters.repositories.vector.client import HarborVectorDBClient
from harborrag_adapters.repositories.vector.qdrant import client as qdrant_client_module
from harborrag_adapters.repositories.vector.qdrant import query as qdrant_query_module
from harborrag_adapters.repositories.vector.qdrant import repository as qdrant_repository_module
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.repository import (
    QdrantVectorRepository,
)


def test_default_clients_register_only_supported_backends() -> None:
    assert HarborDatabaseClient.default().backends() == ("postgresql", "sqlite")
    assert HarborGraphDBClient.default().backends() == ("falkordb",)
    assert HarborStateDBClient.default().backends() == ("redis", "sqlite")
    assert HarborVectorDBClient.default().backends() == ("qdrant",)


class _AsyncConnection:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FalkorDBWithoutDirectClose:
    def __init__(self, **options: Any) -> None:
        del options
        self.connection = _AsyncConnection()

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_falkordb_client_closes_sdk_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        falkordb_client_module,
        "FalkorDB",
        _FalkorDBWithoutDirectClose,
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


def test_database_client_capabilities_create_and_create_from_config() -> None:
    client = HarborDatabaseClient.default()
    assert client.capabilities("sqlite") is None

    created = client.create(backend="sqlite", options={"database": ":memory:"})
    assert isinstance(created, SQLiteDatabaseBackend)

    from_config = client.create_from_config(SQLiteDatabaseConfig(database=":memory:"))
    assert isinstance(from_config, SQLiteDatabaseBackend)


def test_graph_client_capabilities_create_and_create_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _FalkorDBWithoutDirectClose)
    client = HarborGraphDBClient.default()
    assert client.capabilities("falkordb") is None

    created = client.create(backend="falkordb")
    assert isinstance(created, FalkorDBGraphRepository)

    from_config = client.create_from_config(FalkorDBGraphConfig())
    assert isinstance(from_config, FalkorDBGraphRepository)


def test_vector_client_capabilities_create_and_create_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FakeAsyncQdrantClient)
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


def test_state_client_capabilities_create_and_create_from_config() -> None:
    client = HarborStateDBClient.default()
    assert client.capabilities("sqlite") is None

    created = client.create(backend="sqlite", options={"database": ":memory:"})
    assert isinstance(created, SQLiteStateBackend)

    from_config = client.create_from_config(SQLiteStateConfig(database=":memory:"))
    assert isinstance(from_config, SQLiteStateBackend)


def test_cache_client_registers_defaults_and_delegates_all_operations() -> None:
    client = HarborCacheDBClient.default()
    assert client.backends() == ("memory", "redis")
    assert client.capabilities("memory") is None

    created = client.create(backend="memory")
    assert isinstance(created, MemoryCacheBackend)

    from_config = client.create_from_config(MemoryCacheConfig())
    assert isinstance(from_config, MemoryCacheBackend)


def test_object_store_client_capabilities_create_and_create_from_config() -> None:
    client = HarborObjectStoreDBClient.default()
    assert "memory" in client.backends()
    assert client.capabilities("memory") is None

    created = client.create(backend="memory")
    assert isinstance(created, MemoryObjectStore)

    from_config = client.create_from_config(MemoryObjectStoreConfig())
    assert isinstance(from_config, MemoryObjectStore)


# --- Additional coverage: QdrantDBClient (added without touching existing tests) ---


def test_qdrant_client_requires_async_client_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_adapters.repositories.vector.qdrant import (
        client as qdrant_client_module,
    )
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

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


class _FakeAsyncQdrantClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.get_collections_calls = 0

    async def get_collections(self) -> list[str]:
        self.get_collections_calls += 1
        return []

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_qdrant_client_embedded_disk_connect_passes_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_adapters.repositories.vector.qdrant import (
        client as qdrant_client_module,
    )
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FakeAsyncQdrantClient)
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
    from harborrag_adapters.repositories.vector.qdrant import (
        client as qdrant_client_module,
    )
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FakeAsyncQdrantClient)
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
    from harborrag_adapters.repositories.vector.qdrant import (
        client as qdrant_client_module,
    )
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FakeAsyncQdrantClient)
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
    from harborrag_adapters.repositories.vector.qdrant import (
        client as qdrant_client_module,
    )
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    class _CountingAsyncQdrantClient:
        instances = 0

        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            _CountingAsyncQdrantClient.instances += 1

        async def get_collections(self) -> list[str]:
            return []

        async def close(self) -> None:
            pass

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _CountingAsyncQdrantClient)
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

    assert _CountingAsyncQdrantClient.instances == 1


@pytest.mark.asyncio
async def test_qdrant_client_connect_failure_closes_underlying_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_adapters.repositories.vector.qdrant import (
        client as qdrant_client_module,
    )
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    class _FailingAsyncQdrantClient:
        last_instance: _FailingAsyncQdrantClient | None = None

        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.closed = False
            _FailingAsyncQdrantClient.last_instance = self

        async def get_collections(self) -> list[str]:
            raise RuntimeError("boom")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FailingAsyncQdrantClient)
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
    assert _FailingAsyncQdrantClient.last_instance is not None
    assert _FailingAsyncQdrantClient.last_instance.closed is True


def test_qdrant_client_raw_property_raises_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FakeAsyncQdrantClient)
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
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FakeAsyncQdrantClient)
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
    from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
    from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _FakeAsyncQdrantClient)
    client = QdrantDBClient(
        deployment=QdrantDeployment.REMOTE,
        url="http://qdrant.invalid",
        path=None,
        api_key=None,
        prefer_grpc=False,
        operation_timeout_seconds=5,
    )

    assert client.deployment == QdrantDeployment.REMOTE
