"""An answer's ``[k]`` markers become ``[n]`` numbered by first appearance, in one piece or many."""

from __future__ import annotations

import re
from itertools import combinations

import pytest

from harborrag_app.workflow_control.chat.citations import (
    SNIPPET_LIMIT,
    CitationNumbering,
    citation_record,
    snippet,
)
from harborrag_core.domain.retrieval import RetrievalResult

pytestmark = pytest.mark.unit

PASSAGES = tuple(
    RetrievalResult(
        id=f"chk_{index}",
        text=f"passage {index}",
        score=0.5,
        metadata={"document_id": f"doc_{index}", "document_title": f"Doc {index}"},
    )
    for index in range(1, 4)
)
ANSWER = (
    "B [2], A [1], B again [2], none [7] or [0], code a[12345], both [1, 3] and [2–3] and [5, 6]."
)


def _numbered(pieces: list[str]) -> tuple[str, CitationNumbering]:
    numbering = CitationNumbering(PASSAGES)
    text = "".join(numbering.feed(piece) for piece in pieces) + numbering.finish()
    return text, numbering


def test_markers_are_numbered_by_first_appearance_and_unknown_ones_dropped() -> None:
    text, numbering = _numbered([ANSWER])

    assert text == (
        "B [1], A [2], B again [1], none or, code a[12345], both [2][3] and [1][3] and."
    )
    assert [(c.n, c.chunk_id) for c in numbering.citations()] == [
        (1, "chk_2"),
        (2, "chk_1"),
        (3, "chk_3"),
    ]


def test_every_marker_has_a_citation_and_every_citation_a_marker() -> None:
    text, numbering = _numbered([ANSWER])

    markers = {int(found) for found in re.findall(r"\[(\d{1,4})\]", text)}
    assert markers == {citation.n for citation in numbering.citations()}


def test_a_marker_split_across_pieces_is_held_back_until_it_closes() -> None:
    numbering = CitationNumbering(PASSAGES)

    shown = [numbering.feed("see ["), numbering.feed("3"), numbering.feed("] and [")]
    rest = numbering.finish()

    assert shown == ["see", "", " [1] and"]
    assert rest == " ["


def test_every_split_into_up_to_three_pieces_gives_the_same_text() -> None:
    whole = _numbered([ANSWER])[0]
    cuts = range(len(ANSWER) + 1)

    for first, second in combinations(cuts, 2):
        pieces = [ANSWER[:first], ANSWER[first:second], ANSWER[second:]]
        assert _numbered(pieces)[0] == whole, (first, second)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("See also [9].", "See also."),
        ("Both [1, 9] and [9, 8] apply.", "Both [1] and apply."),
        ("Range [1-3] here.", "Range [1][2][3] here."),
        ("Too wide [1-300].", "Too wide."),
    ],
)
def test_lists_and_ranges_are_split_and_dropped_markers_leave_no_gap(
    answer: str, expected: str
) -> None:
    numbering = CitationNumbering(PASSAGES)

    assert numbering.feed(answer) + numbering.finish() == expected


def test_an_answer_without_passages_cites_nothing() -> None:
    numbering = CitationNumbering(())

    assert numbering.feed("Your name is A [1].") + numbering.finish() == "Your name is A."
    assert numbering.citations() == ()


def test_a_snippet_is_collapsed_and_bounded() -> None:
    long = "word " * 200

    assert snippet("  two\n\nlines ") == "two lines"
    assert len(snippet(long)) <= SNIPPET_LIMIT
    assert snippet(long).endswith("…")


def test_a_citation_names_its_document_and_locates_it_when_it_can() -> None:
    result = RetrievalResult(
        id="chk_1",
        text="text",
        score=0.5,
        metadata={
            "document_id": "doc_1",
            "citation_locator": {"uri": "https://example.test/a?secret=1", "page_start": 4},
        },
    )

    citation = citation_record(1, result)

    assert (citation.title, citation.url, citation.page) == (
        "doc_1",
        "https://example.test/a",
        4,
    )
