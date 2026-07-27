from __future__ import annotations

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextRefiner,
    TokenCounter,
)

from ..config import ChunkingProfile
from ..errors import ChunkingError
from ..schemas import ChunkingRequest, ChunkUnit


class GenericChunkingStrategy:
    """Create text units for content without source-specific structure."""

    name = "generic"
    version = "2"

    def __init__(self, token_counter: TokenCounter, refiner: TextRefiner) -> None:
        self._token_counter = token_counter
        self._refiner = refiner

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Refine normalized text into source-ordered structural units."""

        values = [
            element.content
            for element in request.document.content
            if element.content and element.content.strip()
        ]
        content = "\n\n".join(values)
        count = self._token_counter.count(content)
        if not content or count < 1:
            return ()
        element_ids = tuple(
            element.id
            for element in request.document.content
            if element.content and element.content.strip()
        )
        source_span = SourceSpan(element_ids=element_ids)
        splits = self._refiner.split(
            TextRefinementRequest(
                content=content,
                maximum_tokens=profile.target_tokens,
                overlap_tokens=profile.overlap_tokens,
                source_span=source_span,
                boundary_kind=SplitBoundaryKind.DOCUMENT,
                preserve_whitespace=False,
            )
        )
        if not splits:
            raise ChunkingError("generic text refiner returned no splits")
        multiple = len(splits) > 1
        return tuple(
            ChunkUnit(
                anchor="body",
                content=split.content,
                token_count=split.token_count,
                role="text",
                structural_path=(),
                source_span=split.source_span or source_span,
                merge_group="body",
                boundary_kind=split.boundary_kind,
                hard_boundary_before=multiple,
                hard_boundary_after=multiple,
                forced_split=split.forced_split,
                metadata={
                    "source_kind": request.source_kind,
                    "content_type": request.content_type,
                    "generic_part_index": index,
                },
            )
            for index, split in enumerate(splits)
        )
