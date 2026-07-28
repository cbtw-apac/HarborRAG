from __future__ import annotations

from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.repository import (
    QdrantVectorRepository,
)
from harborrag_core.schemas.storage import StorageFamily


class QdrantVectorPlugin(RepositoryPlugin[QdrantVectorConfig, QdrantVectorRepository]):
    """Creates the qdrant implementation without provider-selection conditionals."""

    name = "qdrant"
    family = StorageFamily.VECTOR
    config_type = QdrantVectorConfig
    optional_dependency = "qdrant"

    def create(
        self, config: QdrantVectorConfig, dependencies: RepositoryDependencies
    ) -> QdrantVectorRepository:
        """Build an unconnected repository product for this backend."""
        return QdrantVectorRepository(config, dependencies.telemetry)
