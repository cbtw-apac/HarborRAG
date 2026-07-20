from __future__ import annotations

from harborrag_core.schemas.storage import StorageFamily

from harborrag_adapters.repositories.database.postgresql.config import (
    PostgreSQLDatabaseConfig,
)
from harborrag_adapters.repositories.database.postgresql.repository import (
    PostgreSQLDatabaseBackend,
)
from harborrag_adapters.repositories.plugin import RepositoryDependencies, RepositoryPlugin


class PostgreSQLDatabasePlugin(
    RepositoryPlugin[PostgreSQLDatabaseConfig, PostgreSQLDatabaseBackend]
):
    """Creates the PostgreSQL database implementation."""

    name = "postgresql"
    family = StorageFamily.DATABASE
    config_type = PostgreSQLDatabaseConfig
    optional_dependency = "asyncpg"

    def create(
        self,
        config: PostgreSQLDatabaseConfig,
        dependencies: RepositoryDependencies,
    ) -> PostgreSQLDatabaseBackend:
        return PostgreSQLDatabaseBackend(config, dependencies.telemetry)
