from __future__ import annotations

import pytest

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextSplit,
)


def test_source_span_requires_ordered_complete_pairs() -> None:
    with pytest.raises(ValueError, match="provided together"):
        SourceSpan(start_offset=0)
    with pytest.raises(ValueError, match="invalid source span offset"):
        SourceSpan(start_offset=3, end_offset=2)
    with pytest.raises(ValueError, match="element_ids"):
        SourceSpan(element_ids=("",))


def test_text_refinement_request_enforces_hard_limit_invariants() -> None:
    with pytest.raises(ValueError, match="positive"):
        TextRefinementRequest(content="text", maximum_tokens=0)
    with pytest.raises(ValueError, match="overlap"):
        TextRefinementRequest(content="text", maximum_tokens=4, overlap_tokens=4)
    with pytest.raises(ValueError, match="empty fallback"):
        TextRefinementRequest(
            content="text",
            maximum_tokens=4,
            separators=("\n",),
        )


def test_text_split_preserves_framework_neutral_context() -> None:
    split = TextSplit(
        content="content",
        token_count=2,
        source_span=SourceSpan(start_offset=1, end_offset=8, element_ids=("e1",)),
        boundary_kind=SplitBoundaryKind.SECTION,
        structural_path=("Architecture", "Chunking"),
        prefix="Document: HarborRAG",
    )

    assert split.source_span is not None
    assert split.source_span.element_ids == ("e1",)
    assert split.boundary_kind == SplitBoundaryKind.SECTION


def test_text_refinement_request_normalizes_language_hint() -> None:
    from harborrag_core.contracts.chunking import TextRefinementRequest

    assert TextRefinementRequest(content="x", maximum_tokens=2).language is None
    request = TextRefinementRequest(content="x", maximum_tokens=2, language="  Python ")
    assert request.language == "python"
    with pytest.raises(ValueError, match="language"):
        TextRefinementRequest(content="x", maximum_tokens=2, language="  ")


def test_chunk_header_context_validates_part_numbering() -> None:
    from harborrag_core.contracts.chunking import ChunkHeaderContext

    assert ChunkHeaderContext(part_index=2, part_count=3).part_index == 2
    with pytest.raises(ValueError, match="together"):
        ChunkHeaderContext(part_index=1)
    with pytest.raises(ValueError, match="part"):
        ChunkHeaderContext(part_index=0, part_count=2)
    with pytest.raises(ValueError, match="part"):
        ChunkHeaderContext(part_index=3, part_count=2)
    with pytest.raises(ValueError, match="structural_path"):
        ChunkHeaderContext(structural_path=("ok", " "))


def test_chunk_contextualizer_is_a_runtime_checkable_port() -> None:
    from harborrag_core.contracts.chunking import ChunkContextualizer, ChunkHeaderContext

    class Plain:
        format_version = "test"

        def header(self, context: ChunkHeaderContext, *, maximum_tokens: int | None = None) -> str:
            return context.document_title or ""

        def render(self, header: str, content: str) -> str:
            return f"{header}\n\n{content}" if header else content

    assert isinstance(Plain(), ChunkContextualizer)
