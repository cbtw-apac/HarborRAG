from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel

from harborrag_adapters.repositories.database.base import HarborDatabaseBackend
from harborrag_adapters.repositories.database.postgresql.plugin import (
    PostgreSQLDatabasePlugin,
)
from harborrag_adapters.repositories.database.sqlite.plugin import SQLiteDatabasePlugin
from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.registry import ProviderMap
from harborrag_core.schemas.storage import StorageFamily


class HarborDatabaseClient:
    """Creates durable HarborRAG database backends."""

    family = StorageFamily.DATABASE

    def __init__(self) -> None:
        self._providers = ProviderMap[HarborDatabaseBackend](self.family)

    def register(
        self,
        plugin: RepositoryPlugin[Any, HarborDatabaseBackend],
    ) -> Self:
        """Register one durable database provider."""
        self._providers.register(plugin)
        return self

    def backends(self) -> tuple[str, ...]:
        """Return registered durable database providers."""
        return self._providers.names()

    def capabilities(self, backend: str) -> BaseModel | None:
        """Return capabilities published by one database provider."""
        return self._providers.capabilities(backend)

    def create(
        self,
        *,
        backend: str,
        instance_name: str = "default",
        options: Mapping[str, object] | None = None,
        dependencies: RepositoryDependencies | None = None,
    ) -> HarborDatabaseBackend:
        """Create one unconnected durable database backend."""
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
    ) -> HarborDatabaseBackend:
        """Create one database backend from validated provider configuration."""
        return self._providers.create_from_config(config, dependencies)

    @classmethod
    def default(cls) -> HarborDatabaseClient:
        """Create a client containing all built-in database providers."""
        return cls().register(PostgreSQLDatabasePlugin()).register(SQLiteDatabasePlugin())
