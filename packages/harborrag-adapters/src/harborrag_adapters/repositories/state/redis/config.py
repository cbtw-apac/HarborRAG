from typing import Literal

from pydantic import SecretStr

from harborrag_adapters.repositories.plugin import RepositoryConfig


class RedisStateConfig(RepositoryConfig):
    """Configures Redis workflow state with tenant-aware key namespacing."""

    backend: Literal["redis"] = "redis"
    url: SecretStr
    key_prefix: str = "harborrag:v1:state"
