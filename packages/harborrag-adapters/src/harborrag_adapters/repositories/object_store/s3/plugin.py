from __future__ import annotations

from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.repository import S3ObjectStore
from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_core.storage import StorageFamily


class S3ObjectStorePlugin(RepositoryPlugin[S3ObjectStoreConfig, S3ObjectStore]):
    """Creates the s3 implementation without provider-selection conditionals."""

    name = "s3"
    family = StorageFamily.OBJECT_STORE
    config_type = S3ObjectStoreConfig
    optional_dependency = "s3"

    def create(
        self, config: S3ObjectStoreConfig, dependencies: RepositoryDependencies
    ) -> S3ObjectStore:
        """Build an unconnected repository product for this backend."""
        return S3ObjectStore(config, dependencies.telemetry)
