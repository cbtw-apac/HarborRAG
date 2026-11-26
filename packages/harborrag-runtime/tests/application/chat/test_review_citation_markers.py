"""Renumbering touches only real citation markers, never code, years or subscripts."""

from __future__ import annotations

from itertools import combinations

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.application.chat.citations import CitationNumbering

pytestmark = pytest.mark.unit

PASSAGES = tuple(
    RetrievalResult(
        id=f"chk_{index}",
        text=f"passage {index}",
        score=1.0,
        metadata={"document_id": f"doc_{index}"},
    )
    for index in range(1, 4)
)
REPRODUCED = (
    "Use `first = items[0]` and `second = items[2]` as in [3]. Released in [2024]. Matrix m[1, 2]."
)
FENCED = "The capital[2] is set in:\n```python\nvalues = rows[1, 2]\nfirst = rows[3]\n```\nSee [1]."


def _numbered(pieces: list[str]) -> tuple[str, CitationNumbering]:
    numbering = CitationNumbering(PASSAGES)
    text = "".join(numbering.feed(piece) for piece in pieces) + numbering.finish()
    return text, numbering


def test_citation_numbering_keeps_code_years_and_subscripts_and_renumbers_real_markers() -> None:
    text, numbering = _numbered([REPRODUCED])

    assert text == (
        "Use `first = items[0]` and `second = items[2]` as in [1]. Released in [2024]. "
        "Matrix m[1, 2]."
    )
    assert [(citation.n, citation.chunk_id) for citation in numbering.citations()] == [(1, "chk_3")]


def test_citation_numbering_renumbers_a_single_marker_glued_to_a_word_but_not_fenced_code() -> None:
    text, numbering = _numbered([FENCED])

    assert text == (
        "The capital[1] is set in:\n```python\nvalues = rows[1, 2]\nfirst = rows[3]\n```\nSee [2]."
    )
    assert [citation.chunk_id for citation in numbering.citations()] == ["chk_2", "chk_1"]


@pytest.mark.parametrize("answer", [REPRODUCED, FENCED], ids=["inline-code", "fenced-block"])
def test_citation_numbering_gives_the_same_text_however_the_answer_is_split(answer: str) -> None:
    whole = _numbered([answer])[0]

    for first, second in combinations(range(len(answer) + 1), 2):
        pieces = [answer[:first], answer[first:second], answer[second:]]
        assert _numbered(pieces)[0] == whole, (first, second)
