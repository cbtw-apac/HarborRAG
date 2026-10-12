from __future__ import annotations

from collections.abc import Sequence

import pytest

from harborrag_adapters.chunking._base import BaseTextRefiner, Cut
from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextRefiner,
)
from harborrag_core.ingestion import ChunkValidationError

from .refiner_contract import CharacterCounter, assert_valid_splits, reconstruct


class LineAndSpaceRefiner(BaseTextRefiner):
    """Toy strategy: blank lines are strongest, then newlines, then spaces."""

    name = "toy"
    version = "1"

    def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
        cuts: list[Cut] = []
        text = request.content
        for index, character in enumerate(text):
            if text.startswith("\n\n", index):
                cuts.append(Cut(index + 2, 0))
            elif character == "\n":
                cuts.append(Cut(index + 1, 1))
            elif character == " ":
                cuts.append(Cut(index + 1, 2))
        return cuts


def refiner() -> LineAndSpaceRefiner:
    return LineAndSpaceRefiner(CharacterCounter())


def test_base_refiner_implements_the_core_port() -> None:
    assert isinstance(refiner(), TextRefiner)
    assert refiner().fingerprint() == "toy:1"


def test_fitting_content_is_returned_unchanged() -> None:
    request = TextRefinementRequest(
        content="short text",
        maximum_tokens=50,
        boundary_kind=SplitBoundaryKind.SECTION,
        structural_path=("Intro",),
    )

    (split,) = refiner().split(request)

    assert split.content == "short text"
    assert split.boundary_kind == SplitBoundaryKind.SECTION
    assert not split.forced_split
    assert split.structural_path == ("Intro",)


def test_blank_content_produces_no_splits() -> None:
    assert refiner().split(TextRefinementRequest(content=" \n\t", maximum_tokens=5)) == ()


def test_strongest_boundaries_win_and_siblings_merge_up_to_the_limit() -> None:
    content = "aaaa bbbb\ncccc\n\ndddd eeee"
    request = TextRefinementRequest(content=content, maximum_tokens=16)

    splits = refiner().split(request)

    assert [split.content for split in splits] == ["aaaa bbbb\ncccc\n\n", "dddd eeee"]
    assert_valid_splits(request, splits, CharacterCounter())
    assert all(split.forced_split for split in splits)
    assert all(split.boundary_kind == SplitBoundaryKind.FORCED for split in splits)


def test_merge_never_reaches_across_a_stronger_boundary_than_the_split() -> None:
    content = "aa\n\n" + "bbbb bbbb bbbb bbbb"
    request = TextRefinementRequest(content=content, maximum_tokens=10)

    splits = refiner().split(request)

    assert splits[0].content == "aa\n\n"
    assert_valid_splits(request, splits, CharacterCounter())


def test_a_token_without_cuts_is_force_windowed_within_the_limit() -> None:
    content = "x" * 23
    request = TextRefinementRequest(content=content, maximum_tokens=10)

    splits = refiner().split(request)

    assert [len(split.content) for split in splits] == [10, 10, 3]
    assert reconstruct(request, splits) == content


def test_windowing_never_separates_a_crlf_pair() -> None:
    content = "a" * 9 + "\r\n" + "b" * 9

    class NoCuts(LineAndSpaceRefiner):
        def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
            return ()

    request = TextRefinementRequest(content=content, maximum_tokens=10)
    splits = NoCuts(CharacterCounter()).split(request)

    assert all(not split.content.startswith("\n") for split in splits)
    assert reconstruct(request, splits) == content


def test_spans_rebase_on_the_request_span_with_inclusive_lines() -> None:
    content = "line one\nline two\n\nline three"
    request = TextRefinementRequest(
        content=content,
        maximum_tokens=12,
        source_span=SourceSpan(
            start_offset=100,
            end_offset=100 + len(content),
            start_line=7,
            end_line=10,
            page_start=2,
            page_end=2,
            element_ids=("p1",),
        ),
    )

    splits = refiner().split(request)

    assert_valid_splits(request, splits, CharacterCounter())
    spans = [split.source_span for split in splits]
    assert spans[0] is not None and spans[0].start_offset == 100
    assert spans[0].start_line == 7
    assert spans[-1] is not None and spans[-1].end_line == 10
    assert all(span is not None and span.element_ids == ("p1",) for span in spans)
    assert all(span is not None and span.page_start == 2 for span in spans)


def test_overlap_starts_at_a_cut_and_respects_the_limit() -> None:
    content = "one two three four five six seven"
    request = TextRefinementRequest(
        content=content,
        maximum_tokens=14,
        overlap_tokens=6,
        preserve_whitespace=False,
    )

    splits = refiner().split(request)

    assert all(split.token_count <= 14 for split in splits)
    assert any(
        later.source_span.start_offset < earlier.source_span.end_offset
        for earlier, later in zip(splits, splits[1:], strict=False)
        if earlier.source_span and later.source_span
    )
    assert reconstruct(request, splits) == content


def test_output_is_deterministic() -> None:
    content = "alpha beta\ngamma delta\n\nepsilon zeta eta theta"
    request = TextRefinementRequest(content=content, maximum_tokens=12)

    assert refiner().split(request) == refiner().split(request)


def test_a_counter_that_cannot_fit_one_character_is_a_validation_error() -> None:
    class Greedy:
        def count(self, text: str) -> int:
            return len(text) * 10

    request = TextRefinementRequest(content="abc", maximum_tokens=5)

    with pytest.raises(ChunkValidationError, match="single character"):
        LineAndSpaceRefiner(Greedy()).split(request)


def test_scope_of_merged_pieces_is_their_common_prefix() -> None:
    class Scoped(LineAndSpaceRefiner):
        def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
            return (
                Cut(6, 0, ("class A:", "def a():")),
                Cut(12, 0, ("class A:", "def b():")),
                Cut(18, 0, ("class B:",)),
            )

    request = TextRefinementRequest(content="000000aaaaaabbbbbbcccccc", maximum_tokens=12)

    splits = Scoped(CharacterCounter()).split(request)

    assert [split.content for split in splits] == ["000000aaaaaa", "bbbbbbcccccc"]
    assert [split.prefix for split in splits] == [None, None]

    request = TextRefinementRequest(content="000000aaaaaabbbbbbcccccc", maximum_tokens=6)
    splits = Scoped(CharacterCounter()).split(request)
    assert [split.prefix for split in splits] == [
        None,
        "class A:\ndef a():",
        "class A:\ndef b():",
        "class B:",
    ]
