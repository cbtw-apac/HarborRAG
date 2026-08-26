from typing import Literal

from harborrag_adapters.repositories.plugin import RepositoryConfig


class MemoryCacheConfig(RepositoryConfig):
    """Configures process-local caching and cooperative locks."""

    backend: Literal["memory"] = "memory"
    key_prefix: str = "harborrag:v1"
