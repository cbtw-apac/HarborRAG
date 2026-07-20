from __future__ import annotations

from harborrag_core.schemas.storage import StorageFamily

from harborrag_adapters.repositories.plugin import (
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.state.redis.config import RedisStateConfig
from harborrag_adapters.repositories.state.redis.repository import RedisStateBackend


class RedisStatePlugin(RepositoryPlugin[RedisStateConfig, RedisStateBackend]):
    """Creates the redis implementation without provider-selection conditionals."""

    name = "redis"
    family = StorageFamily.STATE
    config_type = RedisStateConfig
    optional_dependency = "redis"

    def create(
        self, config: RedisStateConfig, dependencies: RepositoryDependencies
    ) -> RedisStateBackend:
        """Build an unconnected repository product for this backend."""
        return RedisStateBackend(config, dependencies.telemetry)
