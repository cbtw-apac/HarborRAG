"""Metadata types emitted by the local connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from harborrag_adapters.connectors.schemas import ConnectorMetadata


@dataclass(slots=True, kw_only=True)
class LocalFileMetadata(ConnectorMetadata):
    """Metadata for one loaded local file.

    Attributes:
        relative_path: POSIX path relative to the trusted root.
        parent_relative_path: POSIX path of the parent directory relative to the root.
        is_symlink: Whether the path was a symlink before resolution.
    """

    source_system: ClassVar[str] = "local"

    relative_path: str
    name: str
    parent_relative_path: str
    suffix: str
    size: int
    accessed_at: datetime
    is_symlink: bool
