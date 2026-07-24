from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel

from harborrag_adapters.repositories.object_store.base import HarborObjectStore
from harborrag_adapters.repositories.object_store.filesystem.plugin import (
    FilesystemObjectStorePlugin,
)
from harborrag_adapters.repositories.object_store.memory.plugin import (
    MemoryObjectStorePlugin,
)
from harborrag_adapters.repositories.object_store.s3.plugin import S3ObjectStorePlugin
from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.shared.provider_map import ProviderMap
from harborrag_core.schemas.storage import StorageFamily


class HarborObjectStoreDBClient:
    """Creates process-local, filesystem, or remote object stores."""

    family = StorageFamily.OBJECT_STORE

    def __init__(self) -> None:
        self._providers = ProviderMap[HarborObjectStore](self.family)

    def register(
        self,
        plugin: RepositoryPlugin[Any, HarborObjectStore],
    ) -> Self:
        """Register one object-store provider."""
        self._providers.register(plugin)
        return self

    def backends(self) -> tuple[str, ...]:
        """Return registered object-store providers."""
        return self._providers.names()

    def capabilities(self, backend: str) -> BaseModel | None:
        """Return capabilities published by one object-store provider."""
        return self._providers.capabilities(backend)

    def create(
        self,
        *,
        backend: str,
        instance_name: str = "default",
        options: Mapping[str, object] | None = None,
        dependencies: RepositoryDependencies | None = None,
    ) -> HarborObjectStore:
        """Create one unconnected object store."""
        return self._providers.create(
            backend=backend,
            instance_name=instance_name,
            options=options,
            dependencies=dependencies,
        )

    def create_from_config(
        self,
        config: RepositoryConfig,
        dependencies: RepositoryDependencies | None = None,
    ) -> HarborObjectStore:
        """Create one object store from validated provider configuration."""
        return self._providers.create_from_config(config, dependencies)

    @classmethod
    def default(cls) -> HarborObjectStoreDBClient:
        """Create a client containing all built-in object-store providers."""
        return (
            cls()
            .register(MemoryObjectStorePlugin())
            .register(FilesystemObjectStorePlugin())
            .register(S3ObjectStorePlugin())
        )
