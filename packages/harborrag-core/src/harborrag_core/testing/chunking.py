"""Deterministic character-based chunking fakes shared across package test suites."""

from __future__ import annotations

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextSplit,
)


class CharacterCounter:
    """One character, one token."""

    def count(self, text: str) -> int:
        return len(text)


class CharacterRefiner:
    """Fixed-width forced splits at ``maximum_tokens``, offsets rebased on the span."""

    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        if not request.content:
            return ()
        results = []
        start = 0
        base = request.source_span
        base_offset = base.start_offset if base and base.start_offset is not None else 0
        while start < len(request.content):
            end = min(start + request.maximum_tokens, len(request.content))
            results.append(
                TextSplit(
                    content=request.content[start:end],
                    token_count=end - start,
                    source_span=SourceSpan(
                        start_offset=base_offset + start,
                        end_offset=base_offset + end,
                        element_ids=base.element_ids if base else (),
                    ),
                    boundary_kind=SplitBoundaryKind.FORCED,
                    structural_path=request.structural_path,
                    forced_split=True,
                )
            )
            start = end
        return tuple(results)
