from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from harborrag_core.chunking import ChunkRecord

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit

type ChunkRecordValidator = Callable[[ChunkRecord, list[str], str], None]


class ChunkStrategy(Protocol):
    """Build source-aware units from one canonical document."""

    name: str
    version: str

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Return deterministic units in canonical source order."""
