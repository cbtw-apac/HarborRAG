from __future__ import annotations

from harborrag_adapters.repositories.object_store.filesystem.config import (
    FilesystemObjectStoreConfig,
)
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_core.storage import StorageFamily


class FilesystemObjectStorePlugin(
    RepositoryPlugin[FilesystemObjectStoreConfig, FilesystemObjectStore]
):
    """Creates the filesystem implementation without provider-selection conditionals."""

    name = "filesystem"
    family = StorageFamily.OBJECT_STORE
    config_type = FilesystemObjectStoreConfig

    def create(
        self, config: FilesystemObjectStoreConfig, dependencies: RepositoryDependencies
    ) -> FilesystemObjectStore:
        """Build an unconnected repository product for this backend."""
        return FilesystemObjectStore(config, dependencies.telemetry)
