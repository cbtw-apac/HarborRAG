from __future__ import annotations

from harborrag_core.schemas.storage import StorageFamily

from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.repository import FalkorDBGraphRepository
from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)


class FalkorDBGraphPlugin(RepositoryPlugin[FalkorDBGraphConfig, FalkorDBGraphRepository]):
    """Creates the falkordb implementation without provider-selection conditionals."""

    name = "falkordb"
    family = StorageFamily.GRAPH
    config_type = FalkorDBGraphConfig
    optional_dependency = "falkordb"

    def create(
        self, config: FalkorDBGraphConfig, dependencies: RepositoryDependencies
    ) -> FalkorDBGraphRepository:
        """Build an unconnected repository product for this backend."""
        return FalkorDBGraphRepository(config, dependencies.telemetry)
