from __future__ import annotations

from harborrag_adapters.repositories.cache.redis.config import RedisCacheConfig
from harborrag_adapters.repositories.cache.redis.repository import RedisCacheBackend
from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_core.schemas.storage import StorageFamily


class RedisCachePlugin(RepositoryPlugin[RedisCacheConfig, RedisCacheBackend]):
    """Creates the redis implementation without provider-selection conditionals."""

    name = "redis"
    family = StorageFamily.CACHE
    config_type = RedisCacheConfig
    optional_dependency = "redis"

    def create(
        self, config: RedisCacheConfig, dependencies: RepositoryDependencies
    ) -> RedisCacheBackend:
        """Build an unconnected repository product for this backend."""
        return RedisCacheBackend(config, dependencies.telemetry)
