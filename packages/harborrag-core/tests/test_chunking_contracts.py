from __future__ import annotations

import pytest

from harborrag_core.contracts.chunking import (
    JsonStructureSplitRequest,
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


def test_json_structure_request_uses_character_bounds_only_for_adapter_structure() -> (
    None
):
    request = JsonStructureSplitRequest(
        value={"items": [{"id": 1}]},
        minimum_characters=10,
        maximum_characters=20,
    )

    assert request.convert_lists
    with pytest.raises(ValueError, match="minimum_characters"):
        JsonStructureSplitRequest(
            value={"id": 1},
            minimum_characters=21,
            maximum_characters=20,
        )
