"""Public chunking use case backed by focused candidate and result stages."""

from __future__ import annotations

from ...base import BaseChunker
from ..config import ChunkingPlan
from ..schemas import ChunkingRequest, ChunkingResult
from .candidates import ChunkCandidatePipeline
from .result import ChunkResultBuilder


class ChunkingService(BaseChunker):
    """Run the deterministic canonical-document chunking pipeline."""

    def __init__(
        self,
        candidates: ChunkCandidatePipeline,
        results: ChunkResultBuilder,
    ) -> None:
        self._candidates = candidates
        self._results = results

    def chunk(
        self,
        request: ChunkingRequest,
        plan: ChunkingPlan | None = None,
    ) -> ChunkingResult:
        """Create and validate canonical chunks for one normalized document."""

        candidates = self._candidates.run(request, plan)
        return self._results.build(request, candidates)
