"""Structured metadata emitted by the local connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from harborrag_adapters.connectors.schemas import ConnectorMetadata


@dataclass(slots=True, kw_only=True)
class LocalFileMetadata(ConnectorMetadata):
    """Structured metadata for one loaded local file."""

    source_system: ClassVar[str] = "local"

    path: str
    relative_path: str
    name: str
    parent_path: str
    suffix: str
    size: int
    accessed_at: datetime
    is_symlink: bool
