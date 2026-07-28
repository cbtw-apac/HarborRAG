from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from harborrag_runtime.config.settings import RuntimeSettings


def test_general_runtime_settings_do_not_require_worker_state_configuration() -> None:
    settings = RuntimeSettings(
        env="prod",
        control_db_url="postgresql+asyncpg://user:pass@database/control",
    )

    assert settings.ingestion_state_backend == "postgresql"


def test_built_in_temporal_worker_requires_postgresql_url() -> None:
    settings = RuntimeSettings()

    with pytest.raises(ValueError, match="built-in Temporal workers require PostgreSQL"):
        settings.require_postgresql_ingestion_state()


def test_sqlite_worker_state_is_rejected() -> None:
    with pytest.raises(ValidationError, match="postgresql"):
        RuntimeSettings(ingestion_state_backend="sqlite")  # type: ignore[arg-type]


def test_postgresql_worker_state_requires_asyncpg_url() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        RuntimeSettings(
            ingestion_state_backend="postgresql",
            ingestion_state_url=SecretStr("postgresql://user:pass@database/state"),
        )


def test_postgresql_worker_state_returns_secret_url() -> None:
    url = SecretStr("postgresql+asyncpg://user:pass@database/state?ssl=require")
    settings = RuntimeSettings(
        ingestion_state_backend="postgresql",
        ingestion_state_url=url,
    )

    assert settings.require_postgresql_ingestion_state() is url
