from __future__ import annotations

from ipaddress import ip_address
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig


class PostgreSQLStateConfig(RepositoryConfig):
    """Configure shared operational state through PostgreSQL/asyncpg."""

    backend: Literal["postgresql"] = "postgresql"
    url: SecretStr
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_recycle_seconds: int = Field(default=1800, ge=0)
    echo: bool = False
    create_schema: bool = False
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_async_url(self) -> PostgreSQLStateConfig:
        parsed = urlsplit(self.url.get_secret_value())
        if parsed.scheme != "postgresql+asyncpg" or not parsed.hostname:
            raise ValueError("PostgreSQL state URL must use postgresql+asyncpg")
        query = parse_qs(parsed.query)
        ssl_mode = query.get("ssl", [""])[-1].lower()
        if (
            not _is_loopback(parsed.hostname)
            and ssl_mode not in {"require", "verify-ca", "verify-full"}
            and not self.allow_insecure_remote
        ):
            raise ValueError(
                "remote PostgreSQL state requires TLS via ?ssl=require (or a "
                "verifying mode); set allow_insecure_remote only for an explicitly "
                "trusted development network"
            )
        return self


def _is_loopback(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
