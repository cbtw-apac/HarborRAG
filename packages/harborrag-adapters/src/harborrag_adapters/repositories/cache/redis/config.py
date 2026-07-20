from typing import Literal

from pydantic import SecretStr

from harborrag_adapters.repositories.plugin import RepositoryConfig


class RedisCacheConfig(RepositoryConfig):
    """Configures Redis caching, counters, tags, and lease-based locks."""

    backend: Literal["redis"] = "redis"
    url: SecretStr
    key_prefix: str = "harborrag:v1"
