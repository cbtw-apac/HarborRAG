from __future__ import annotations

import pytest

from harborrag_adapters.repositories.cache.client import HarborCacheDBClient
from harborrag_adapters.repositories.cache.memory.backend import MemoryCacheBackend
from harborrag_adapters.repositories.cache.memory.config import MemoryCacheConfig
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

from .fakes import FakeAsyncQdrantClient, FalkorDBWithoutDirectClose


def test_default_clients_register_only_supported_backends() -> None:
    assert HarborDatabaseClient.default().backends() == ("postgresql", "sqlite")
    assert HarborGraphDBClient.default().backends() == ("falkordb",)
    assert HarborStateDBClient.default().backends() == ("redis", "sqlite")
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
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBWithoutDirectClose)
    client = HarborGraphDBClient.default()
    assert client.capabilities("falkordb") is None

    created = client.create(backend="falkordb")
    assert isinstance(created, FalkorDBGraphRepository)

    from_config = client.create_from_config(FalkorDBGraphConfig())
    assert isinstance(from_config, FalkorDBGraphRepository)


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
