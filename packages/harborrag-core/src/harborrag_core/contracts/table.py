from __future__ import annotations

from typing import Protocol, runtime_checkable

from harborrag_core.domain.table import TableArtifact


@runtime_checkable
class TableArtifactRepository(Protocol):
    """Persistence boundary for exact canonical table versions."""

    async def put(self, artifact: TableArtifact) -> None:
        """Persist one immutable table artifact version."""

    async def get(self, table_version_id: str) -> TableArtifact | None:
        """Load one exact table artifact version when present."""
