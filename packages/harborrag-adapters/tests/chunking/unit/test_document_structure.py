from __future__ import annotations

from harborrag_adapters.chunking.document_structure import (
    DocumentStructureRefiner,
    fenced_ranges,
    looks_like_markdown,
)
from harborrag_core.contracts.chunking import TextRefinementRequest

from .refiner_contract import CharacterCounter, assert_valid_splits, reconstruct


def split_document(content: str, maximum: int) -> list[str]:
    counter = CharacterCounter()
    request = TextRefinementRequest(content=content, maximum_tokens=maximum)
    splits = DocumentStructureRefiner(counter).split(request)
    assert_valid_splits(request, splits, counter)
    assert reconstruct(request, splits) == content
    return [split.content for split in splits]


def test_headings_are_the_strongest_boundary() -> None:
    content = "# One\nAlpha text. More text.\n## Two\nBeta text here.\n"

    pieces = split_document(content, 30)

    assert pieces[0] == "# One\nAlpha text. More text.\n"
    assert pieces[1].startswith("## Two")


def test_setext_heading_starts_a_new_piece() -> None:
    content = "Intro paragraph text\nTitle\n=====\nBody under the title.\n"

    pieces = split_document(content, 30)

    assert any(piece.startswith("Title\n=====") for piece in pieces)


def test_a_fence_is_kept_whole_when_it_fits() -> None:
    fence = "```python\ndef f():\n    return 1\n```\n"
    content = "Some intro words here.\n" + fence + "Closing words after code."

    pieces = split_document(content, len(fence) + 2)

    assert fence in pieces


def test_an_oversized_fence_is_cut_only_at_its_line_breaks() -> None:
    lines = [f"line_{index} = {index}\n" for index in range(12)]
    content = "```\n" + "".join(lines) + "```\n"

    pieces = split_document(content, 40)

    assert all(piece.endswith("\n") for piece in pieces)


def test_list_items_are_not_split_when_they_fit() -> None:
    items = [f"- item number {index} with words\n" for index in range(6)]
    content = "".join(items)

    pieces = split_document(content, 70)

    for piece in pieces:
        assert piece.startswith("- ")
        assert piece.endswith("\n")


def test_fenced_ranges_cover_open_fence_to_end() -> None:
    content = "text\n~~~\ncode\n"

    assert fenced_ranges(content) == [(5, len(content))]


def test_markdown_detection() -> None:
    assert looks_like_markdown("# Title\nbody")
    assert looks_like_markdown("```\ncode\n```")
    assert looks_like_markdown("- a\n- b\n")
    assert not looks_like_markdown("Plain prose. With - a dash inside.")
