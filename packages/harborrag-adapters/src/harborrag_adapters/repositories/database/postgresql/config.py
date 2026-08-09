from __future__ import annotations

from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig
from harborrag_core.security import is_loopback_host


class PostgreSQLDatabaseConfig(RepositoryConfig):
    """Configures PostgreSQL through SQLAlchemy's asynchronous asyncpg dialect."""

    backend: Literal["postgresql"] = "postgresql"
    url: SecretStr
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_recycle_seconds: int = Field(default=1800, ge=0)
    echo: bool = False
    create_schema: bool = False
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_async_url(self) -> PostgreSQLDatabaseConfig:
        parsed = urlsplit(self.url.get_secret_value())
        if parsed.scheme != "postgresql+asyncpg" or not parsed.hostname:
            raise ValueError("PostgreSQL URL must use postgresql+asyncpg")
        ssl_mode = parse_qs(parsed.query).get("ssl", [""])[-1].lower()
        if (
            ssl_mode not in {"require", "verify-ca", "verify-full"}
            and not is_loopback_host(parsed.hostname)
            and not self.allow_insecure_remote
        ):
            raise ValueError(
                "remote PostgreSQL database requires TLS via ?ssl=require (or a "
                "verifying mode); set allow_insecure_remote only for an explicitly "
                "trusted development network"
            )
        return self
