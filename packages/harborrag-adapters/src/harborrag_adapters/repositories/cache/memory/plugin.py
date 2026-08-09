from __future__ import annotations

from harborrag_adapters.repositories.cache.memory.backend import MemoryCacheBackend
from harborrag_adapters.repositories.cache.memory.config import MemoryCacheConfig
from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_core.storage import StorageFamily


class MemoryCachePlugin(RepositoryPlugin[MemoryCacheConfig, MemoryCacheBackend]):
    """Builds the in-process Redis-compatible cache backend."""

    name = "memory"
    family = StorageFamily.CACHE
    config_type = MemoryCacheConfig
    optional_dependency = "memory"

    def create(
        self, config: MemoryCacheConfig, dependencies: RepositoryDependencies
    ) -> MemoryCacheBackend:
        """Build an unconnected memory cache repository composition."""
        return MemoryCacheBackend(
            instance_name=config.instance_name,
            key_prefix=config.key_prefix,
            telemetry=dependencies.telemetry,
        )
