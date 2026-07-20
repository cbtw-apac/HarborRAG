from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harborrag_adapters.repositories.cache.redis.config import RedisCacheConfig
from harborrag_adapters.repositories.cache.redis.plugin import RedisCachePlugin
from harborrag_adapters.repositories.cache.redis.repository import RedisCacheBackend
from harborrag_adapters.repositories.database.sqlite.config import SQLiteDatabaseConfig
from harborrag_adapters.repositories.database.sqlite.plugin import SQLiteDatabasePlugin
from harborrag_adapters.repositories.database.sqlite.repository import SQLiteDatabaseBackend
from harborrag_adapters.repositories.graph.falkordb import client as falkordb_client_module
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.plugin import FalkorDBGraphPlugin
from harborrag_adapters.repositories.graph.falkordb.repository import FalkorDBGraphRepository
from harborrag_adapters.repositories.object_store.filesystem.config import (
    FilesystemObjectStoreConfig,
)
from harborrag_adapters.repositories.object_store.filesystem.plugin import (
    FilesystemObjectStorePlugin,
)
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_adapters.repositories.object_store.memory.config import MemoryObjectStoreConfig
from harborrag_adapters.repositories.object_store.memory.plugin import MemoryObjectStorePlugin
from harborrag_adapters.repositories.object_store.memory.repository import MemoryObjectStore
from harborrag_adapters.repositories.object_store.s3 import client as s3_client_module
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.plugin import S3ObjectStorePlugin
from harborrag_adapters.repositories.object_store.s3.repository import S3ObjectStore
from harborrag_adapters.repositories.plugin import RepositoryDependencies
from harborrag_adapters.repositories.shared import redis as shared_redis_module
from harborrag_adapters.repositories.state.redis.config import RedisStateConfig
from harborrag_adapters.repositories.state.redis.plugin import RedisStatePlugin
from harborrag_adapters.repositories.state.redis.repository import RedisStateBackend
from harborrag_adapters.repositories.state.sqlite.config import SQLiteStateConfig
from harborrag_adapters.repositories.state.sqlite.plugin import SQLiteStatePlugin
from harborrag_adapters.repositories.state.sqlite.repository import SQLiteStateBackend
from harborrag_adapters.repositories.vector.qdrant import client as qdrant_client_module
from harborrag_adapters.repositories.vector.qdrant import repository as qdrant_repository_module
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.plugin import QdrantVectorPlugin
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository


class _Sentinel:
    """Stands in for an optional provider SDK module without touching the network."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


def test_sqlite_database_plugin_builds_unconnected_backend() -> None:
    plugin = SQLiteDatabasePlugin()
    backend = plugin.create(SQLiteDatabaseConfig(), RepositoryDependencies())
    assert isinstance(backend, SQLiteDatabaseBackend)


def test_sqlite_state_plugin_builds_unconnected_backend() -> None:
    plugin = SQLiteStatePlugin()
    backend = plugin.create(SQLiteStateConfig(), RepositoryDependencies())
    assert isinstance(backend, SQLiteStateBackend)


def test_memory_object_store_plugin_builds_unconnected_backend() -> None:
    plugin = MemoryObjectStorePlugin()
    store = plugin.create(MemoryObjectStoreConfig(), RepositoryDependencies())
    assert isinstance(store, MemoryObjectStore)


def test_filesystem_object_store_plugin_builds_unconnected_backend(tmp_path: Path) -> None:
    plugin = FilesystemObjectStorePlugin()
    store = plugin.create(
        FilesystemObjectStoreConfig(root=tmp_path),
        RepositoryDependencies(),
    )
    assert isinstance(store, FilesystemObjectStore)


def test_redis_cache_plugin_builds_unconnected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_redis_module, "redis", _Sentinel())
    plugin = RedisCachePlugin()
    backend = plugin.create(
        RedisCacheConfig(url="redis://localhost:6379/0"),
        RepositoryDependencies(),
    )
    assert isinstance(backend, RedisCacheBackend)


def test_redis_state_plugin_builds_unconnected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_redis_module, "redis", _Sentinel())
    plugin = RedisStatePlugin()
    backend = plugin.create(
        RedisStateConfig(url="redis://localhost:6379/0"),
        RepositoryDependencies(),
    )
    assert isinstance(backend, RedisStateBackend)


def test_falkordb_graph_plugin_builds_unconnected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", _Sentinel)
    plugin = FalkorDBGraphPlugin()
    repository = plugin.create(FalkorDBGraphConfig(), RepositoryDependencies())
    assert isinstance(repository, FalkorDBGraphRepository)


def test_qdrant_vector_plugin_builds_unconnected_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _Sentinel)
    monkeypatch.setattr(qdrant_repository_module, "qm", _Sentinel())
    plugin = QdrantVectorPlugin()
    repository = plugin.create(
        QdrantVectorConfig(url="http://qdrant.invalid"),
        RepositoryDependencies(),
    )
    assert isinstance(repository, QdrantVectorRepository)


def test_s3_object_store_plugin_builds_unconnected_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_client_module, "aioboto3", _Sentinel(Session=_Sentinel))
    plugin = S3ObjectStorePlugin()
    store = plugin.create(S3ObjectStoreConfig(), RepositoryDependencies())
    assert isinstance(store, S3ObjectStore)
