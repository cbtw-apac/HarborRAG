"""Deterministic, structure-first splitting for prose.

Cuts are tried strongest first: paragraph breaks, then sentence ends, then line
breaks, then clause punctuation, then plain whitespace. Sentence detection is
rule-based so the same text always splits the same way:

* ``.``, ``!`` and ``?`` end a sentence only when whitespace follows, so
  decimals (``3.14``) and dotted names (``a.b``) never split;
* a period after a known abbreviation (``e.g.``, ``Dr.``) or a single-letter
  initial does not end a sentence;
* CJK full stops (``。！？．``) end a sentence without needing a space.

The delimiter always stays with the text before it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from harborrag_core.contracts.chunking import TextRefinementRequest

from ._base import BaseTextRefiner, Cut

PARAGRAPH = 0
SENTENCE = 1
LINE = 2
CLAUSE = 3
WHITESPACE = 4
TEXT_PRIORITY_LEVELS = 5

_ABBREVIATIONS = frozenset(
    {
        "e.g",
        "i.e",
        "etc",
        "vs",
        "cf",
        "al",
        "approx",
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "no",
        "fig",
        "eq",
        "vol",
        "pp",
        "inc",
        "ltd",
        "co",
        "corp",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
    }
)

_BLANK_LINES = re.compile(r"\n[ \t\r]*\n[ \t\r\n]*")
_LATIN_SENTENCE_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s)\s+")
_CJK_SENTENCE_END = re.compile(r"[。！？．]+[」』）】”’]*\s*")
_LINE_BREAK = re.compile(r"\n")
_LATIN_CLAUSE = re.compile(r"[;:,]+(?=\s)\s+")
_CJK_CLAUSE = re.compile(r"[；：，、]+\s*")
_WHITESPACE = re.compile(r"\s+")
_WORD_BEFORE = re.compile(r"([A-Za-z]+(?:\.[A-Za-z]+)*)$")


def text_cuts(content: str, start: int = 0, end: int | None = None, *, base: int = 0) -> list[Cut]:
    """Return prose cut positions in ``content[start:end]``.

    ``base`` shifts every priority so another strategy can rank these cuts
    below its own structural ones.
    """

    stop = len(content) if end is None else end
    cuts: list[Cut] = []

    def add(pattern: re.Pattern[str], priority: int, *, sentence: bool = False) -> None:
        for match in pattern.finditer(content, start, stop):
            if sentence and _is_abbreviation(content, match.start()):
                continue
            cuts.append(Cut(match.end(), base + priority))

    add(_BLANK_LINES, PARAGRAPH)
    add(_LATIN_SENTENCE_END, SENTENCE, sentence=True)
    add(_CJK_SENTENCE_END, SENTENCE)
    add(_LINE_BREAK, LINE)
    add(_LATIN_CLAUSE, CLAUSE)
    add(_CJK_CLAUSE, CLAUSE)
    add(_WHITESPACE, WHITESPACE)
    return cuts


def _is_abbreviation(content: str, delimiter: int) -> bool:
    """Report whether the period at ``delimiter`` closes an abbreviation or initial."""

    if content[delimiter] != ".":
        return False
    if delimiter + 1 < len(content) and content[delimiter + 1] in ".!?":
        return False
    match = _WORD_BEFORE.search(content, max(0, delimiter - 32), delimiter)
    if match is None:
        return False
    word = match.group(1)
    return (len(word) == 1 and word.isupper()) or word.lower() in _ABBREVIATIONS


class TextStructureRefiner(BaseTextRefiner):
    """Split oversized prose at the strongest sentence-level boundary that fits."""

    name = "text-structure"
    version = "1"

    def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
        return text_cuts(request.content)
