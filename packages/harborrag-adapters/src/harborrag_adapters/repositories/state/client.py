from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel

from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.shared.provider_map import ProviderMap
from harborrag_adapters.repositories.state.base import HarborStateBackend
from harborrag_adapters.repositories.state.redis.plugin import RedisStatePlugin
from harborrag_adapters.repositories.state.sqlite.plugin import SQLiteStatePlugin
from harborrag_core.schemas.storage import StorageFamily


class HarborStateDBClient:
    """Creates operational state backends for checkpoints, workflows, and leases."""

    family = StorageFamily.STATE

    def __init__(self) -> None:
        self._providers = ProviderMap[HarborStateBackend](self.family)

    def register(
        self,
        plugin: RepositoryPlugin[Any, HarborStateBackend],
    ) -> Self:
        """Register one operational state provider."""
        self._providers.register(plugin)
        return self

    def backends(self) -> tuple[str, ...]:
        """Return registered operational state providers."""
        return self._providers.names()

    def capabilities(self, backend: str) -> BaseModel | None:
        """Return capabilities published by one state provider."""
        return self._providers.capabilities(backend)

    def create(
        self,
        *,
        backend: str,
        instance_name: str = "default",
        options: Mapping[str, object] | None = None,
        dependencies: RepositoryDependencies | None = None,
    ) -> HarborStateBackend:
        """Create one unconnected operational state backend."""
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
    ) -> HarborStateBackend:
        """Create one state backend from validated provider configuration."""
        return self._providers.create_from_config(config, dependencies)

    @classmethod
    def default(cls) -> HarborStateDBClient:
        """Create a client containing all built-in state providers."""
        return cls().register(SQLiteStatePlugin()).register(RedisStatePlugin())
