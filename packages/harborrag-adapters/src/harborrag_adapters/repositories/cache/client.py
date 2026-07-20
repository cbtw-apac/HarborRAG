from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from harborrag_core.schemas.storage import StorageFamily
from pydantic import BaseModel

from harborrag_adapters.repositories.cache.base import HarborCacheBackend
from harborrag_adapters.repositories.cache.memory.plugin import MemoryCachePlugin
from harborrag_adapters.repositories.cache.redis.plugin import RedisCachePlugin
from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.shared.provider_map import ProviderMap


class HarborCacheDBClient:
    """Creates process-local or distributed cache backends."""

    family = StorageFamily.CACHE

    def __init__(self) -> None:
        self._providers = ProviderMap[HarborCacheBackend](self.family)

    def register(
        self,
        plugin: RepositoryPlugin[Any, HarborCacheBackend],
    ) -> Self:
        """Register one cache provider."""
        self._providers.register(plugin)
        return self

    def backends(self) -> tuple[str, ...]:
        """Return registered cache providers."""
        return self._providers.names()

    def capabilities(self, backend: str) -> BaseModel | None:
        """Return capabilities published by one cache provider."""
        return self._providers.capabilities(backend)

    def create(
        self,
        *,
        backend: str,
        instance_name: str = "default",
        options: Mapping[str, object] | None = None,
        dependencies: RepositoryDependencies | None = None,
    ) -> HarborCacheBackend:
        """Create one unconnected cache backend."""
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
    ) -> HarborCacheBackend:
        """Create one cache backend from validated provider configuration."""
        return self._providers.create_from_config(config, dependencies)

    @classmethod
    def default(cls) -> HarborCacheDBClient:
        """Create a client containing all built-in cache providers."""
        return cls().register(MemoryCachePlugin()).register(RedisCachePlugin())
