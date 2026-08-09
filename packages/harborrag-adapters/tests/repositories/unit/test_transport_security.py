"""Secure-by-default transport validation for remote adapter backends."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from harborrag_adapters.models.runtime.redis_config import RedisConnectionConfig
from harborrag_adapters.repositories.cache.redis.config import RedisCacheConfig
from harborrag_adapters.repositories.database.postgresql.config import (
    PostgreSQLDatabaseConfig,
)
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.state.redis.config import RedisStateConfig
from harborrag_core.security import is_loopback_host

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize("host", ["localhost", "localhost.", "127.0.0.2", "::1"])
def test_loopback_host_recognizes_local_aliases(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("config_type", [RedisCacheConfig, RedisStateConfig])
def test_repository_redis_requires_tls_for_remote_hosts(
    config_type: type[RedisCacheConfig] | type[RedisStateConfig],
) -> None:
    with pytest.raises(ValidationError, match="requires an encrypted transport"):
        config_type(url=SecretStr("redis://user:secret@redis.example.com/0"))

    assert config_type(url=SecretStr("rediss://user:secret@redis.example.com/0"))
    assert config_type(
        url=SecretStr("redis://redis.internal:6379/0"),
        allow_insecure_remote=True,
    )
    assert config_type(url=SecretStr("unix:///run/redis/redis.sock"))


def test_model_runtime_redis_requires_tls_for_remote_hosts() -> None:
    with pytest.raises(ValidationError, match="requires an encrypted transport"):
        RedisConnectionConfig(url="redis://user:secret@redis.example.com/0")

    assert RedisConnectionConfig(url="rediss://user:secret@redis.example.com/0")
    assert RedisConnectionConfig(
        url="redis://redis.internal:6379/0",
        allow_insecure_remote=True,
    )


def test_postgresql_database_requires_tls_for_remote_hosts() -> None:
    remote = "postgresql+asyncpg://user:secret@database.example.com/harbor"
    with pytest.raises(ValidationError, match="requires TLS"):
        PostgreSQLDatabaseConfig(url=SecretStr(remote))

    assert PostgreSQLDatabaseConfig(url=SecretStr(f"{remote}?ssl=verify-full"))
    assert PostgreSQLDatabaseConfig(url=SecretStr(remote), allow_insecure_remote=True)
    assert PostgreSQLDatabaseConfig(
        url=SecretStr("postgresql+asyncpg://user:secret@localhost/harbor")
    )


def test_s3_requires_https_for_remote_custom_endpoints() -> None:
    with pytest.raises(ValidationError, match="requires an encrypted transport"):
        S3ObjectStoreConfig(endpoint_url="http://minio.example.com:9000")

    assert S3ObjectStoreConfig(endpoint_url="https://minio.example.com:9000")
    assert S3ObjectStoreConfig(
        endpoint_url="http://minio:9000",
        allow_insecure_remote=True,
    )
    assert S3ObjectStoreConfig(endpoint_url="http://127.0.0.1:9000")


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://minio.example.com/bucket",
        "https://user:secret@minio.example.com",
        "https://minio.example.com?token=secret",
        "https://minio.example.com#fragment",
    ],
)
def test_s3_rejects_malformed_or_secret_bearing_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="S3"):
        S3ObjectStoreConfig(endpoint_url=endpoint)
