from __future__ import annotations

import pytest
from harborrag_adapters.repositories.database.postgresql import repository as pg_repository_module
from harborrag_adapters.repositories.database.postgresql.config import PostgreSQLDatabaseConfig
from harborrag_adapters.repositories.database.postgresql.plugin import PostgreSQLDatabasePlugin
from harborrag_adapters.repositories.database.postgresql.repository import (
    PostgreSQLDatabaseBackend,
)
from harborrag_adapters.repositories.plugin import RepositoryDependencies
from pydantic import ValidationError


def test_config_accepts_a_valid_asyncpg_url() -> None:
    config = PostgreSQLDatabaseConfig(url="postgresql+asyncpg://user:pass@localhost/db")
    assert config.url.get_secret_value() == "postgresql+asyncpg://user:pass@localhost/db"
    assert config.pool_size == 5
    assert config.max_overflow == 10
    assert config.create_schema is False


def test_config_rejects_a_non_asyncpg_url() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        PostgreSQLDatabaseConfig(url="postgresql://user:pass@localhost/db")


def test_plugin_creates_an_unconnected_backend() -> None:
    plugin = PostgreSQLDatabasePlugin()
    config = PostgreSQLDatabaseConfig(url="postgresql+asyncpg://user:pass@localhost/db")
    backend = plugin.create(config, RepositoryDependencies())
    assert isinstance(backend, PostgreSQLDatabaseBackend)


def test_backend_configures_an_asyncpg_sqlalchemy_client() -> None:
    config = PostgreSQLDatabaseConfig(
        url="postgresql+asyncpg://user:pass@localhost/db",
        pool_size=2,
        max_overflow=4,
        pool_recycle_seconds=60,
        echo=True,
    )
    backend = PostgreSQLDatabaseBackend(config)
    assert backend._database.backend == "postgresql"


def test_backend_raises_import_error_when_asyncpg_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_repository_module, "find_spec", lambda name: None)
    config = PostgreSQLDatabaseConfig(url="postgresql+asyncpg://user:pass@localhost/db")
    with pytest.raises(ImportError, match="asyncpg is not installed"):
        PostgreSQLDatabaseBackend(config)
