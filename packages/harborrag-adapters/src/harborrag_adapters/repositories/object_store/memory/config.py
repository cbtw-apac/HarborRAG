from typing import Literal

from harborrag_adapters.repositories.plugin import RepositoryConfig


class MemoryObjectStoreConfig(RepositoryConfig):
    """Configures deterministic process-local object storage."""

    backend: Literal["memory"] = "memory"
