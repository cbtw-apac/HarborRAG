from __future__ import annotations

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
)
from harborrag_core.testing.chunking import CharacterCounter, CharacterRefiner


def test_character_counter_counts_characters() -> None:
    assert CharacterCounter().count("") == 0
    assert CharacterCounter().count("abcde") == 5


def test_character_refiner_splits_fixed_width_with_rebased_offsets() -> None:
    request = TextRefinementRequest(
        content="abcdefgh",
        maximum_tokens=3,
        source_span=SourceSpan(start_offset=10, end_offset=18, element_ids=("e1", "e2")),
        structural_path=("Doc", "Section"),
    )
    splits = CharacterRefiner().split(request)
    assert [split.content for split in splits] == ["abc", "def", "gh"]
    assert [split.token_count for split in splits] == [3, 3, 2]
    assert [(s.source_span.start_offset, s.source_span.end_offset) for s in splits] == [
        (10, 13),
        (13, 16),
        (16, 18),
    ]
    for split in splits:
        assert split.boundary_kind is SplitBoundaryKind.FORCED
        assert split.forced_split is True
        assert split.source_span.element_ids == ("e1", "e2")
        assert split.structural_path == ("Doc", "Section")


def test_character_refiner_handles_empty_content_and_missing_span() -> None:
    assert CharacterRefiner().split(TextRefinementRequest(content="", maximum_tokens=3)) == ()
    splits = CharacterRefiner().split(TextRefinementRequest(content="ab", maximum_tokens=5))
    assert len(splits) == 1
    assert (splits[0].source_span.start_offset, splits[0].source_span.end_offset) == (0, 2)
    assert splits[0].source_span.element_ids == ()
