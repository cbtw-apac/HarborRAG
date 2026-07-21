from typing import Literal

from pydantic import Field, SecretStr

from harborrag_adapters.repositories.plugin import RepositoryConfig


class FalkorDBGraphConfig(RepositoryConfig):
    """Configures the official async FalkorDB client and selected graph key."""

    backend: Literal["falkordb"] = "falkordb"
    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    graph_name: str = "harborrag"
    ssl: bool = False
    max_connections: int = Field(default=32, ge=1, le=1000)
