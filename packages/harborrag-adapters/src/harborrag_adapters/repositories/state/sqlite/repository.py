from __future__ import annotations

from harborrag_adapters.repositories.state.sql import SQLStateBackend
from harborrag_adapters.repositories.state.sqlite.client import SQLiteStateDBClient
from harborrag_adapters.repositories.state.sqlite.config import SQLiteStateConfig
from harborrag_adapters.repositories.telemetry import StorageTelemetryHook


class SQLiteStateBackend(SQLStateBackend):
    """Composes operational state stores over embedded SQLite."""

    def __init__(
        self,
        config: SQLiteStateConfig,
        telemetry: StorageTelemetryHook | None = None,
        *,
        client: SQLiteStateDBClient | None = None,
    ) -> None:
        super().__init__(
            client=client or SQLiteStateDBClient(database=config.database),
            instance_name=config.instance_name,
            create_schema=config.create_schema,
            telemetry=telemetry,
        )
