from __future__ import annotations

from importlib.util import find_spec

from harborrag_adapters.repositories.database.postgresql.config import (
    PostgreSQLDatabaseConfig,
)
from harborrag_adapters.repositories.database.sqlalchemy import (
    SQLAlchemyDatabaseBackend,
)
from harborrag_adapters.repositories.shared.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.telemetry import StorageTelemetryHook


class PostgreSQLDatabaseBackend(SQLAlchemyDatabaseBackend):
    """Persists canonical HarborRAG records in PostgreSQL through asyncpg."""

    def __init__(
        self,
        config: PostgreSQLDatabaseConfig,
        telemetry: StorageTelemetryHook | None = None,
    ) -> None:
        if find_spec("asyncpg") is None:
            raise ImportError("asyncpg is not installed")
        super().__init__(
            client=SQLAlchemyDBClient(
                backend="postgresql",
                url=config.url.get_secret_value(),
                pool_size=config.pool_size,
                max_overflow=config.max_overflow,
                pool_recycle_seconds=config.pool_recycle_seconds,
                echo=config.echo,
            ),
            instance_name=config.instance_name,
            create_schema=config.create_schema,
            telemetry=telemetry,
        )
