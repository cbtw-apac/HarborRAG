from __future__ import annotations

from harborrag_adapters.repositories.database.sqlalchemy import (
    SQLAlchemyDatabaseBackend,
)
from harborrag_adapters.repositories.database.sqlite.client import SQLiteDBClient
from harborrag_adapters.repositories.database.sqlite.config import SQLiteDatabaseConfig
from harborrag_adapters.repositories.telemetry import StorageTelemetryHook


class SQLiteDatabaseBackend(SQLAlchemyDatabaseBackend):
    """Composes database repositories over a local SQLite database client."""

    def __init__(
        self,
        config: SQLiteDatabaseConfig,
        telemetry: StorageTelemetryHook | None = None,
        *,
        client: SQLiteDBClient | None = None,
    ) -> None:
        database = client or SQLiteDBClient(
            database=config.database,
            echo=config.echo,
        )
        super().__init__(
            client=database,
            instance_name=config.instance_name,
            create_schema=config.create_schema,
            telemetry=telemetry,
        )
