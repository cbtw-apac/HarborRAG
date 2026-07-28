from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from harborrag_adapters.repositories.state.postgresql import (
    repository as pg_repository_module,
)
from harborrag_adapters.repositories.state.postgresql.config import (
    PostgreSQLStateConfig,
)
from harborrag_adapters.repositories.state.postgresql.repository import (
    PostgreSQLStateBackend,
)


def test_config_accepts_loopback_asyncpg_url_without_tls() -> None:
    config = PostgreSQLStateConfig(
        url=SecretStr("postgresql+asyncpg://user:pass@localhost/harborrag")
    )

    assert config.url.get_secret_value().endswith("@localhost/harborrag")
    assert "pass" not in repr(config)


def test_config_rejects_non_asyncpg_url() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        PostgreSQLStateConfig(url=SecretStr("postgresql://user:pass@localhost/harborrag"))


def test_config_requires_tls_for_remote_postgresql() -> None:
    with pytest.raises(ValidationError, match="requires TLS"):
        PostgreSQLStateConfig(
            url=SecretStr("postgresql+asyncpg://user:pass@database.example/harborrag")
        )


@pytest.mark.parametrize("ssl_mode", ["require", "verify-ca", "verify-full"])
def test_config_accepts_remote_postgresql_with_tls(ssl_mode: str) -> None:
    config = PostgreSQLStateConfig(
        url=SecretStr(f"postgresql+asyncpg://user:pass@database.example/harborrag?ssl={ssl_mode}")
    )

    assert config.allow_insecure_remote is False


def test_config_allows_explicit_trusted_network_opt_out() -> None:
    config = PostgreSQLStateConfig(
        url=SecretStr("postgresql+asyncpg://user:pass@postgres/harborrag"),
        allow_insecure_remote=True,
    )

    assert config.allow_insecure_remote is True


def test_backend_configures_shared_sql_state_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_repository_module, "find_spec", lambda name: object())
    config = PostgreSQLStateConfig(
        url=SecretStr("postgresql+asyncpg://user:pass@postgres/harborrag"),
        pool_size=3,
        max_overflow=7,
        allow_insecure_remote=True,
    )

    backend = PostgreSQLStateBackend(config)

    assert backend.client.backend == "postgresql"
    assert backend.client._pool_size == 3
    assert backend.client._max_overflow == 7


def test_backend_requires_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_repository_module, "find_spec", lambda name: None)
    config = PostgreSQLStateConfig(
        url=SecretStr("postgresql+asyncpg://user:pass@localhost/harborrag")
    )

    with pytest.raises(ImportError, match="asyncpg is not installed"):
        PostgreSQLStateBackend(config)


@pytest.mark.asyncio
async def test_schema_creation_is_serialized_for_worker_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_repository_module, "find_spec", lambda name: object())
    backend = PostgreSQLStateBackend(
        PostgreSQLStateConfig(
            url=SecretStr("postgresql+asyncpg://user:pass@postgres/harborrag"),
            allow_insecure_remote=True,
        )
    )
    calls: list[tuple[str, Any]] = []

    class Connection:
        async def execute(self, statement: Any, parameters: Any) -> None:
            calls.append((str(statement), parameters))

        async def run_sync(self, operation: Any) -> None:
            calls.append(("run_sync", operation))

    class Transaction:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *args: object) -> None:
            return None

    backend.client = SimpleNamespace(  # type: ignore[assignment]
        raw=SimpleNamespace(begin=Transaction)
    )

    await backend._initialize_schema()

    assert calls[0][0] == "SELECT pg_advisory_xact_lock(:lock_id)"
    assert calls[0][1] == {"lock_id": pg_repository_module._SCHEMA_LOCK_ID}
    assert calls[1][0] == "run_sync"
