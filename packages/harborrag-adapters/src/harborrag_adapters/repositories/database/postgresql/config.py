from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig


class PostgreSQLDatabaseConfig(RepositoryConfig):
    """Configures PostgreSQL through SQLAlchemy's asynchronous asyncpg dialect."""

    backend: Literal["postgresql"] = "postgresql"
    url: SecretStr
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_recycle_seconds: int = Field(default=1800, ge=0)
    echo: bool = False
    create_schema: bool = False

    @model_validator(mode="after")
    def validate_async_url(self) -> PostgreSQLDatabaseConfig:
        if not self.url.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("PostgreSQL URL must use postgresql+asyncpg")
        return self
