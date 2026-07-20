from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from harborrag_core.schemas.storage import StorageFamily
from pydantic import BaseModel

from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.shared.provider_map import ProviderMap
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_adapters.repositories.vector.qdrant.plugin import QdrantVectorPlugin


class HarborVectorDBClient:
    """Creates vector database repositories from registered providers."""

    family = StorageFamily.VECTOR

    def __init__(self) -> None:
        self._providers = ProviderMap[HarborVectorRepository](self.family)

    def register(
        self,
        plugin: RepositoryPlugin[Any, HarborVectorRepository],
    ) -> Self:
        """Register one vector database provider."""
        self._providers.register(plugin)
        return self

    def backends(self) -> tuple[str, ...]:
        """Return registered vector database providers."""
        return self._providers.names()

    def capabilities(self, backend: str) -> BaseModel | None:
        """Return capabilities published by one vector provider."""
        return self._providers.capabilities(backend)

    def create(
        self,
        *,
        backend: str,
        instance_name: str = "default",
        options: Mapping[str, object] | None = None,
        dependencies: RepositoryDependencies | None = None,
    ) -> HarborVectorRepository:
        """Create one unconnected vector repository."""
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
    ) -> HarborVectorRepository:
        """Create one vector repository from validated provider configuration."""
        return self._providers.create_from_config(config, dependencies)

    @classmethod
    def default(cls) -> HarborVectorDBClient:
        """Create a client containing all built-in vector providers."""
        return cls().register(QdrantVectorPlugin())
