from __future__ import annotations

from harborrag_core.schemas.storage import StorageFamily

from harborrag_adapters.repositories.object_store.memory.config import MemoryObjectStoreConfig
from harborrag_adapters.repositories.object_store.memory.repository import MemoryObjectStore
from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)


class MemoryObjectStorePlugin(RepositoryPlugin[MemoryObjectStoreConfig, MemoryObjectStore]):
    """Builds the deterministic process-local object store."""

    name = "memory"
    family = StorageFamily.OBJECT_STORE
    config_type = MemoryObjectStoreConfig

    def create(
        self, config: MemoryObjectStoreConfig, dependencies: RepositoryDependencies
    ) -> MemoryObjectStore:
        """Build an unconnected memory object store."""
        return MemoryObjectStore(
            instance_name=config.instance_name,
            telemetry=dependencies.telemetry,
        )
