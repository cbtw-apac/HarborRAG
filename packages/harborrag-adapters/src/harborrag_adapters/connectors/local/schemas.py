"""Structured metadata emitted by the local connector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(slots=True)
class LocalFileMetadata:
    """Structured metadata for one loaded local file."""

    source_system: str
    path: str
    relative_path: str
    name: str
    parent_path: str
    suffix: str
    size: int
    checksum: str
    created_at: datetime
    updated_at: datetime
    accessed_at: datetime
    is_symlink: bool

    def to_dict(self) -> dict[str, object]:
        """Serialize metadata for ``RawDocument.metadata``."""
        return asdict(self)
