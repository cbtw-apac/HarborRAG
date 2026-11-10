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

    def plan_for_max_tokens(self, connector_type: str, max_tokens: int) -> ChunkingPlan:
        """A plan capping this run's chunks at `max_tokens`; see `ChunkCandidatePipeline`."""

        return self._candidates.plan_for_max_tokens(connector_type, max_tokens)
