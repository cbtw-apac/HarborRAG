from __future__ import annotations

import pytest

from harborrag_adapters.chunking.text_structure import TextStructureRefiner, text_cuts
from harborrag_core.contracts.chunking import SourceSpan, TextRefinementRequest

from .refiner_contract import CharacterCounter, WordCounter, assert_valid_splits, reconstruct


def split_text(content: str, maximum: int, counter=None) -> list[str]:
    counter = counter or CharacterCounter()
    request = TextRefinementRequest(content=content, maximum_tokens=maximum)
    splits = TextStructureRefiner(counter).split(request)
    assert_valid_splits(request, splits, counter)
    assert reconstruct(request, splits) == content
    return [split.content for split in splits]


def test_paragraph_breaks_are_preferred_over_sentences() -> None:
    first = "First paragraph has one. And two sentences."
    second = "Second paragraph."
    content = f"{first}\n\n{second}"

    assert split_text(content, len(first) + 3) == [f"{first}\n\n", second]


def test_sentences_split_after_their_punctuation() -> None:
    content = "Alpha is first. Beta is second! Gamma is third?"

    assert split_text(content, 20) == ["Alpha is first. ", "Beta is second! ", "Gamma is third?"]


@pytest.mark.parametrize(
    "content",
    [
        "Use tools e.g. hammers and saws for this job",
        "Ask Dr. Smith about the results right away",
        "Written by J. R. Tolkien in the last century",
        "The ratio is 3.14 and the limit is 2.5 units",
    ],
)
def test_abbreviations_initials_and_decimals_do_not_end_sentences(content: str) -> None:
    sentence_cuts = [cut for cut in text_cuts(content) if cut.priority == 1]

    assert sentence_cuts == []


def test_cjk_sentences_split_without_spaces() -> None:
    content = "今天天气很好。我们去公园散步！你想一起来吗？"

    assert split_text(content, 8) == ["今天天气很好。", "我们去公园散步！", "你想一起来吗？"]


def test_cjk_full_width_question_and_exclamation_are_sentence_ends() -> None:
    content = "好吗？好！"
    priorities = {cut.offset: cut.priority for cut in text_cuts(content)}

    assert priorities == {3: 1, len(content): 1}


def test_clauses_are_used_when_a_sentence_is_too_long() -> None:
    content = "one clause here; another clause there, and a third one"

    pieces = split_text(content, 22)

    assert pieces[0] == "one clause here; "


def test_a_single_huge_word_is_force_windowed() -> None:
    content = "short " + "x" * 40 + " end"

    pieces = split_text(content, 16)

    assert all(len(piece) <= 16 for piece in pieces)


def test_word_counter_limits_are_respected() -> None:
    content = " ".join(f"Sentence number {index} is here." for index in range(30))

    pieces = split_text(content, 20, WordCounter())

    assert all(WordCounter().count(piece) <= 20 for piece in pieces)
    assert all(piece.rstrip().endswith(".") for piece in pieces)


def test_crlf_paragraphs_split_cleanly() -> None:
    content = "First line here.\r\n\r\nSecond block here."

    assert split_text(content, 20) == ["First line here.\r\n\r\n", "Second block here."]


def test_spans_follow_a_rebased_request() -> None:
    content = "Alpha one.\nBeta two.\nGamma three."
    request = TextRefinementRequest(
        content=content,
        maximum_tokens=12,
        source_span=SourceSpan(
            start_offset=40, end_offset=40 + len(content), start_line=3, end_line=5
        ),
    )

    splits = TextStructureRefiner(CharacterCounter()).split(request)

    assert [
        (s.source_span.start_line, s.source_span.end_line) for s in splits if s.source_span
    ] == [
        (3, 3),
        (4, 4),
        (5, 5),
    ]
