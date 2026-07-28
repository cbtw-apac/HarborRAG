from __future__ import annotations

from importlib.util import find_spec

from sqlalchemy import text

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.state.postgresql.config import (
    PostgreSQLStateConfig,
)
from harborrag_adapters.repositories.state.sql_backend import SQLStateBackend
from harborrag_adapters.repositories.state.sql_schema import _METADATA
from harborrag_adapters.repositories.telemetry import StorageTelemetryHook

_SCHEMA_LOCK_ID = int.from_bytes(b"HarborRG", byteorder="big", signed=True)


class PostgreSQLStateBackend(SQLStateBackend):
    """Share checkpoints, leases, and workflow metadata across worker replicas."""

    def __init__(
        self,
        config: PostgreSQLStateConfig,
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

    async def _initialize_schema(self) -> None:
        """Serialize replica schema creation with a transaction-scoped lock."""

        async with self.client.raw.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _SCHEMA_LOCK_ID},
            )
            await connection.run_sync(_METADATA.create_all)
