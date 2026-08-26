from __future__ import annotations

from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.state.sqlite.config import SQLiteStateConfig
from harborrag_adapters.repositories.state.sqlite.repository import SQLiteStateBackend
from harborrag_core.storage import StorageFamily


class SQLiteStatePlugin(RepositoryPlugin[SQLiteStateConfig, SQLiteStateBackend]):
    """Creates the sqlite implementation without provider-selection conditionals."""

    name = "sqlite"
    family = StorageFamily.STATE
    config_type = SQLiteStateConfig

    def create(
        self, config: SQLiteStateConfig, dependencies: RepositoryDependencies
    ) -> SQLiteStateBackend:
        """Build an unconnected repository product for this backend."""
        return SQLiteStateBackend(config, dependencies.telemetry)
