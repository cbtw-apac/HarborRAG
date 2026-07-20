from pathlib import Path
from typing import Literal

from harborrag_adapters.repositories.plugin import RepositoryConfig


class FilesystemObjectStoreConfig(RepositoryConfig):
    """Configures a local object store constrained to a trusted root."""

    backend: Literal["filesystem"] = "filesystem"
    root: Path
