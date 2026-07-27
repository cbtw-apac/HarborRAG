from __future__ import annotations

from typing import Protocol

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit


class ChunkStrategy(Protocol):
    """Contract for producing identity-free structural chunk units."""

    name: str
    version: str

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Create identity-free structural units in source order."""
