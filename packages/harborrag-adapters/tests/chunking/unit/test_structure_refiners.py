"""Text, document and separator refiners: structural cuts, limits and coverage."""

from __future__ import annotations

from harborrag_adapters.chunking import (
    DocumentStructureRefiner,
    SeparatorRefiner,
    TextStructureRefiner,
)
from harborrag_core.contracts.chunking import SourceSpan, SplitBoundaryKind, TextRefinementRequest


def test_text_refiner_cuts_at_sentence_ends_but_not_inside_abbreviations_or_decimals(
    words, assert_tiles
) -> None:
    text = (
        "Dr. Smith arrived at 3.14 pm. He met e.g. the team. "
        "Then everyone left the building quickly. It rained."
    )
    request = TextRefinementRequest(content=text, maximum_tokens=8)

    splits = TextStructureRefiner(words).split(request)

    assert [split.content for split in splits] == [
        "Dr. Smith arrived at 3.14 pm. ",
        "He met e.g. the team. ",
        "Then everyone left the building quickly. It rained.",
    ]
    assert {split.boundary_kind for split in splits} == {SplitBoundaryKind.SENTENCE}
    assert not any(split.forced_split for split in splits)
    assert_tiles(request, splits)


def test_text_refiner_returns_fitting_content_unchanged(words) -> None:
    request = TextRefinementRequest(
        content="short text",
        maximum_tokens=5,
        boundary_kind=SplitBoundaryKind.PARAGRAPH,
        structural_path=("Guide",),
    )

    (split,) = TextStructureRefiner(words).split(request)

    assert split.content == "short text"
    assert split.boundary_kind == SplitBoundaryKind.PARAGRAPH
    assert split.structural_path == ("Guide",)


def test_text_refiner_drops_blank_content(words) -> None:
    assert TextStructureRefiner(words).split(TextRefinementRequest("  \n ", 3)) == ()


def test_text_refiner_windows_a_run_without_cuts_and_marks_it_forced(assert_tiles) -> None:
    class Characters:
        def count(self, text: str) -> int:
            return len(text)

    request = TextRefinementRequest(content="x" * 25, maximum_tokens=10)

    splits = TextStructureRefiner(Characters()).split(request)

    assert [len(split.content) for split in splits] == [10, 10, 5]
    assert all(split.forced_split for split in splits)
    assert {split.boundary_kind for split in splits} == {SplitBoundaryKind.FORCED}
    assert_tiles(request, splits)


def test_text_refiner_overlap_starts_at_a_cut_and_respects_the_limit(words) -> None:
    text = "one two three. four five six. seven eight nine. ten eleven twelve."
    request = TextRefinementRequest(content=text, maximum_tokens=6, overlap_tokens=2)

    splits = TextStructureRefiner(words).split(request)

    assert len(splits) > 1
    assert all(split.token_count <= 6 for split in splits)
    for previous, current in zip(splits, splits[1:], strict=False):
        assert previous.source_span is not None and current.source_span is not None
        assert current.source_span.start_offset <= previous.source_span.end_offset
        assert current.content[:1] != " "


def test_document_refiner_cuts_before_headings_and_keeps_fences_whole(words, assert_tiles) -> None:
    markdown = (
        "# Intro\n\nSome intro words here today.\n\n"
        "## Part A\n\nAlpha beta gamma delta epsilon.\n\n"
        "```python\nx = 1\ny = 2\n```\n\n"
        "## Part B\n\nZeta eta theta iota kappa."
    )
    request = TextRefinementRequest(content=markdown, maximum_tokens=10)

    splits = DocumentStructureRefiner(words).split(request)

    assert [split.content for split in splits] == [
        "# Intro\n\nSome intro words here today.\n\n",
        "## Part A\n\nAlpha beta gamma delta epsilon.\n\n",
        "```python\nx = 1\ny = 2\n```\n\n",
        "## Part B\n\nZeta eta theta iota kappa.",
    ]
    assert_tiles(request, splits)


def test_document_refiner_joins_a_bare_heading_to_the_section_it_introduces(words) -> None:
    markdown = (
        "Lead paragraph with several words in it.\n\n"
        "# Chapter\n\n"
        "## Section\n\nBody text of the section follows here."
    )

    splits = DocumentStructureRefiner(words).split(
        TextRefinementRequest(content=markdown, maximum_tokens=12)
    )

    assert splits[-1].content.startswith("# Chapter\n\n## Section")


def test_separator_refiner_cuts_at_line_breaks_and_maps_lines(words, assert_tiles) -> None:
    content = "a b c\nd e f\ng h i\nj k l\n"
    request = TextRefinementRequest(
        content=content,
        maximum_tokens=6,
        source_span=SourceSpan(start_offset=40, end_offset=64, start_line=5, end_line=8),
        boundary_kind=SplitBoundaryKind.LINE,
        separators=("\n", ""),
    )

    splits = SeparatorRefiner(words).split(request)

    assert [split.content for split in splits] == ["a b c\nd e f\n", "g h i\nj k l\n"]
    assert [(split.source_span.start_line, split.source_span.end_line) for split in splits] == [
        (5, 6),
        (7, 8),
    ]
    assert {split.boundary_kind for split in splits} == {SplitBoundaryKind.LINE}
    assert_tiles(request, splits)
