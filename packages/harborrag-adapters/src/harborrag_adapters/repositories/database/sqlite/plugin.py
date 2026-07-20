from __future__ import annotations

from harborrag_core.schemas.storage import StorageFamily

from harborrag_adapters.repositories.database.sqlite.config import SQLiteDatabaseConfig
from harborrag_adapters.repositories.database.sqlite.repository import (
    SQLiteDatabaseBackend,
)
from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)


class SQLiteDatabasePlugin(RepositoryPlugin[SQLiteDatabaseConfig, SQLiteDatabaseBackend]):
    """Creates the sqlite implementation without provider-selection conditionals."""

    name = "sqlite"
    family = StorageFamily.DATABASE
    config_type = SQLiteDatabaseConfig

    def create(
        self, config: SQLiteDatabaseConfig, dependencies: RepositoryDependencies
    ) -> SQLiteDatabaseBackend:
        """Build an unconnected repository product for this backend."""
        return SQLiteDatabaseBackend(config, dependencies.telemetry)
