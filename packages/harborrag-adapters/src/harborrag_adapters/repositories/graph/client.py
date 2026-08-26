from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel

from harborrag_adapters.repositories.graph.base import HarborGraphRepository
from harborrag_adapters.repositories.graph.falkordb.plugin import FalkorDBGraphPlugin
from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.registry import ProviderMap
from harborrag_core.storage import StorageFamily


class HarborGraphDBClient:
    """Creates embedded or remote graph database repositories."""

    family = StorageFamily.GRAPH

    def __init__(self) -> None:
        self._providers = ProviderMap[HarborGraphRepository](self.family)

    def register(
        self,
        plugin: RepositoryPlugin[Any, HarborGraphRepository],
    ) -> Self:
        """Register one graph database provider."""
        self._providers.register(plugin)
        return self

    def backends(self) -> tuple[str, ...]:
        """Return registered graph database providers."""
        return self._providers.names()

    def capabilities(self, backend: str) -> BaseModel | None:
        """Return capabilities published by one graph provider."""
        return self._providers.capabilities(backend)

    def create(
        self,
        *,
        backend: str,
        instance_name: str = "default",
        options: Mapping[str, object] | None = None,
        dependencies: RepositoryDependencies | None = None,
    ) -> HarborGraphRepository:
        """Create one unconnected graph repository."""
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
    ) -> HarborGraphRepository:
        """Create one graph repository from validated provider configuration."""
        return self._providers.create_from_config(config, dependencies)

    @classmethod
    def default(cls) -> HarborGraphDBClient:
        """Create a client containing all built-in graph providers."""
        return cls().register(FalkorDBGraphPlugin())
