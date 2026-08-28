from __future__ import annotations

import os

import pytest
from pydantic import SecretStr, ValidationError

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import RuntimeConfigurationError


@pytest.fixture(autouse=True)
def _no_ambient_harborrag_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide these cases from their arguments, never from the ambient environment.

    RuntimeSettings reads the whole HARBORRAG_ prefix, so an exported variable or a
    dotenv file loaded by some other test module can satisfy a key these tests assert
    is missing -- which is exactly how they came to pass alone and fail in the suite.
    """

    for name in [key for key in os.environ if key.startswith("HARBORRAG_")]:
        monkeypatch.delenv(name, raising=False)


def test_default_tenant_is_operator_readable() -> None:
    assert RuntimeSettings().ingestion_tenant_id == "DEFAULT"


def test_control_database_pool_settings_are_bounded() -> None:
    settings = RuntimeSettings(
        env="prod",
        control_db_url="postgresql+asyncpg://user:pass@database/control",
        secrets_encryption_key="test-encryption-key",
        control_db_pool_size=12,
        control_db_max_overflow=24,
    )

    assert settings.control_db_pool_size == 12
    assert settings.control_db_max_overflow == 24

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        RuntimeSettings(control_db_pool_size=0)


def test_temporal_worker_capacity_is_configurable_and_cannot_overcommit_pool() -> None:
    settings = RuntimeSettings(
        control_db_pool_size=12,
        control_db_max_overflow=12,
        temporal_max_concurrent_activities=4,
        temporal_max_concurrent_workflow_tasks=7,
        temporal_max_concurrent_activity_polls=3,
        temporal_max_concurrent_workflow_polls=3,
        temporal_graceful_shutdown_seconds=45,
    )

    assert settings.temporal_max_concurrent_activities == 4
    assert settings.temporal_graceful_shutdown_seconds == 45
    worker = TemporalRuntimeConfig.from_settings(settings).worker
    assert worker.max_concurrent_activities == 4
    assert worker.max_concurrent_workflow_tasks == 7
    assert worker.max_concurrent_activity_polls == 3
    assert worker.max_concurrent_workflow_polls == 3
    assert worker.graceful_shutdown_seconds == 45

    constrained = RuntimeSettings(
        control_db_pool_size=5,
        control_db_max_overflow=0,
        temporal_max_concurrent_activities=2,
    )
    with pytest.raises(RuntimeConfigurationError, match="exceeds the control database pool"):
        TemporalRuntimeConfig.from_settings(constrained)


def test_redis_url_is_secret_and_accepts_tls_scheme() -> None:
    settings = RuntimeSettings(
        control_db_url="postgresql+asyncpg://user:database-secret@database/control",
        secrets_encryption_key="test-encryption-key",
        temporal_api_key="temporal-secret",
        redis_url=SecretStr("rediss://user:private@redis.example.com/0"),
        qdrant_api_key="qdrant-secret",
        falkordb_password="graph-secret",
    )

    rendered = repr(settings)
    assert all(
        secret not in rendered
        for secret in (
            "database-secret",
            "temporal-secret",
            "private",
            "qdrant-secret",
            "graph-secret",
        )
    )
    assert settings.redis_url is not None


def test_redis_url_rejects_non_redis_scheme() -> None:
    with pytest.raises(ValidationError, match="HARBORRAG_REDIS_URL"):
        RuntimeSettings(redis_url=SecretStr("https://redis.example.com"))


def test_remote_plaintext_redis_requires_development_acknowledgement() -> None:
    remote_url = SecretStr("redis://user:private@redis.internal:6379/0")

    with pytest.raises(ValidationError, match="remote Redis requires an encrypted transport"):
        RuntimeSettings(redis_url=remote_url)

    acknowledged = RuntimeSettings(
        redis_url=remote_url,
        redis_allow_insecure_remote=True,
    )
    assert acknowledged.redis_url == remote_url

    with pytest.raises(ValidationError, match="remote Redis requires an encrypted transport"):
        RuntimeSettings(
            env="prod",
            control_db_url="postgresql+asyncpg://database/control",
            secrets_encryption_key="test-encryption-key",
            redis_url=remote_url,
            redis_allow_insecure_remote=True,
        )


def test_remote_plaintext_object_store_requires_development_acknowledgement() -> None:
    endpoint = "http://minio.internal:9000"

    with pytest.raises(ValidationError, match="remote object store requires"):
        RuntimeSettings(
            object_store_endpoint_url=endpoint,
            object_store_access_key_id="local-access-key",
            object_store_secret_access_key="local-secret-key",
        )

    acknowledged = RuntimeSettings(
        object_store_endpoint_url=endpoint,
        object_store_access_key_id="local-access-key",
        object_store_secret_access_key="local-secret-key",
        object_store_allow_insecure_remote=True,
    )
    assert acknowledged.object_store_endpoint_url == endpoint

    with pytest.raises(ValidationError, match="remote object store requires"):
        RuntimeSettings(
            env="prod",
            control_db_url="postgresql+asyncpg://database/control",
            secrets_encryption_key="test-encryption-key",
            object_store_endpoint_url=endpoint,
            object_store_allow_insecure_remote=True,
        )


def test_remote_secure_object_store_is_accepted_in_production() -> None:
    settings = RuntimeSettings(
        env="prod",
        control_db_url="postgresql+asyncpg://database/control",
        secrets_encryption_key="test-encryption-key",
        object_store_endpoint_url="https://objects.example.com",
    )

    assert settings.object_store_endpoint_url == "https://objects.example.com"


def test_ingestion_metrics_settings_validate_the_listener_port() -> None:
    settings = RuntimeSettings(
        metrics_port=9464,
        metrics_bind_address="127.0.0.1",
        langfuse_enabled=True,
    )

    assert settings.metrics_port == 9464
    assert settings.metrics_bind_address == "127.0.0.1"
    assert settings.langfuse_enabled is True

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        RuntimeSettings(metrics_port=0)


def test_default_sqlite_control_db_does_not_require_an_encryption_key() -> None:
    settings = RuntimeSettings(secrets_encryption_key=None)

    assert settings.secrets_encryption_key is None


def test_non_sqlite_control_db_requires_an_explicit_encryption_key_even_in_dev() -> None:
    """env=dev with a real Postgres DSN is legal but must not fall back to the
    publicly-known dev-default Fernet key for stored secrets."""
    with pytest.raises(ValidationError, match="HARBORRAG_SECRETS_ENCRYPTION_KEY must be set"):
        RuntimeSettings(
            control_db_url="postgresql+asyncpg://user:pass@database/control",
            secrets_encryption_key=None,
        )

    settings = RuntimeSettings(
        control_db_url="postgresql+asyncpg://user:pass@database/control",
        secrets_encryption_key="test-encryption-key",
    )
    assert settings.secrets_encryption_key is not None


def test_prod_requires_an_explicit_encryption_key() -> None:
    with pytest.raises(ValidationError, match="HARBORRAG_SECRETS_ENCRYPTION_KEY must be set"):
        RuntimeSettings(
            env="prod",
            control_db_url="postgresql+asyncpg://user:pass@database/control",
            secrets_encryption_key=None,
        )


def test_prod_rejects_a_blank_encryption_key() -> None:
    """A blank key is not None, so it must not slip past the "must be set"
    check and reach Fernet key derivation as a fixed, guessable value."""
    with pytest.raises(ValidationError, match="HARBORRAG_SECRETS_ENCRYPTION_KEY must be set"):
        RuntimeSettings(
            env="prod",
            control_db_url="postgresql+asyncpg://user:pass@database/control",
            secrets_encryption_key="   ",
        )


def test_graph_concurrency_settings_are_positive_and_independent() -> None:
    settings = RuntimeSettings(
        falkordb_max_connections=24,
        graph_relation_repair_concurrency=6,
    )

    assert settings.falkordb_max_connections == 24
    assert settings.graph_relation_repair_concurrency == 6

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        RuntimeSettings(graph_relation_repair_concurrency=0)
