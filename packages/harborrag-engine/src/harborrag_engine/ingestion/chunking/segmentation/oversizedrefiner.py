from __future__ import annotations

from dataclasses import replace

from harborrag_core.contracts.chunking import (
    TextRefinementRequest,
    TextRefiner,
)

from ..config import ChunkingProfile
from ..errors import OversizedChunkError
from ..schemas import ChunkUnit
from .tablechunker import TableChunker


class OversizedUnitRefiner:
    """Refine one unit without crossing its strategy-owned boundary."""

    _LINE_PRESERVING_ROLES = frozenset({"code", "json", "log", "table"})

    def __init__(self, refiner: TextRefiner, table_chunker: TableChunker) -> None:
        self._refiner = refiner
        self._table_chunker = table_chunker

    def refine(
        self,
        units: tuple[ChunkUnit, ...],
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Refine every oversized unit while preserving source order."""

        results: list[ChunkUnit] = []
        for unit in units:
            results.extend(self.refine_one(unit, profile))
        return tuple(results)

    def refine_one(
        self,
        unit: ChunkUnit,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Refine one unit until every result satisfies the hard limit."""

        if unit.token_count <= profile.maximum_tokens:
            return (unit,)
        if unit.role == "table":
            splits = self._table_chunker.split(unit, profile)
            self._validate(splits, profile)
            return splits

        line_preserving = unit.role in self._LINE_PRESERVING_ROLES
        text_splits = self._refiner.split(
            TextRefinementRequest(
                content=unit.content,
                maximum_tokens=profile.maximum_tokens,
                overlap_tokens=0 if line_preserving else profile.overlap_tokens,
                source_span=unit.source_span,
                boundary_kind=unit.boundary_kind,
                structural_path=unit.structural_path,
                preserve_whitespace=line_preserving,
                separators=("\n", "") if line_preserving else None,
            )
        )
        refined = tuple(
            replace(
                unit,
                content=split.content,
                token_count=split.token_count,
                source_span=split.source_span or unit.source_span,
                merge_group=f"{unit.merge_group}:part:{index}",
                boundary_kind=split.boundary_kind,
                hard_boundary_before=True,
                hard_boundary_after=True,
                forced_split=True,
                metadata={**unit.metadata, "local_part_index": index},
            )
            for index, split in enumerate(text_splits)
        )
        self._validate(refined, profile)
        return refined

    @staticmethod
    def _validate(
        units: tuple[ChunkUnit, ...],
        profile: ChunkingProfile,
    ) -> None:
        if not units:
            raise OversizedChunkError("oversized refiner returned no units")
        if any(unit.token_count > profile.maximum_tokens for unit in units):
            raise OversizedChunkError("refiner returned a unit above maximum_tokens")
