"""Number an answer's citations by first appearance and describe each cited passage.

The model cites passage ``k`` of its prompt as ``[k]``, or several as ``[k, m]`` or
``[k-m]``. The caller sees ``[n]``, where ``n`` counts the passages in the order the
answer first cites them, one number per bracket, so every citation is cited. Only a
marker whose every number names a passage is rewritten. Every other bracket is left
as written: a year such as ``[2024]``, a list or range glued to a word such as
``m[1, 2]``, and anything inside inline code or a fenced code block.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_memory.messages import CitationRecord

from .presenters import citation_uri, display_text

SNIPPET_LIMIT = 300
_MAX_RANGE = 20
_ITEM = r"\d{1,4}(?:\s*[-–]\s*\d{1,4})?"
_MARKER = re.compile(rf"\[\s*({_ITEM}(?:\s*,\s*{_ITEM})*)\s*\]")
_HELD = re.compile(r"(?:\s*\[[\d\s,\-–]{0,40}|\s+|`+)$")
_CODE_DELIMITER = re.compile(r"`+")
_LIST_OR_RANGE = re.compile(r"[,\-–]")
_ELLIPSIS = "…"


class CitationNumbering:
    """Rewrite one answer's markers as it arrives, in one piece or many.

    ``feed`` returns the text that can be shown so far; a marker or a run of backticks
    split across pieces, and the whitespace before it, is held back until it closes.
    ``finish`` returns what is left. The concatenated output is the answer as stored and
    returned.

    Args:
        passages: The passages of the prompt, in the order the model saw them.
    """

    def __init__(self, passages: Sequence[RetrievalResult]) -> None:
        self._passages = tuple(passages)
        self._numbers: dict[int, int] = {}
        self._pending = ""
        self._code_delimiter = ""
        self._previous = ""

    def feed(self, text: str) -> str:
        """Return the renumbered text that no later piece can change."""
        buffered = self._pending + text
        held = _HELD.search(buffered)
        cut = held.start() if held else len(buffered)
        self._pending = buffered[cut:]
        return self._rewrite(buffered[:cut])

    def finish(self) -> str:
        """Return the held-back rest of the answer."""
        rest, self._pending = self._pending, ""
        return self._rewrite(rest)

    def citations(self) -> tuple[CitationRecord, ...]:
        """Return the cited passages, ``n`` ascending."""
        order = sorted(self._numbers.items(), key=lambda item: item[1])
        return tuple(citation_record(n, self._passages[index - 1]) for index, n in order)

    def _rewrite(self, text: str) -> str:
        """Renumber the markers of ``text`` that lie outside code, carrying code state on."""
        pieces: list[str] = []
        start = 0
        for delimiter in _CODE_DELIMITER.finditer(text):
            pieces.append(self._rewrite_prose(text[start : delimiter.start()], text, start))
            pieces.append(delimiter.group())
            self._toggle_code(delimiter.group())
            start = delimiter.end()
        pieces.append(self._rewrite_prose(text[start:], text, start))
        self._previous = (self._previous + text)[-1:]
        return "".join(pieces)

    def _rewrite_prose(self, segment: str, text: str, start: int) -> str:
        """Renumber ``segment``, which starts at ``start`` in ``text``, unless it is code."""
        if self._code_delimiter:
            return segment
        before = text[start - 1] if start else self._previous
        return _MARKER.sub(self._renumber, before + segment)[len(before) :]

    def _toggle_code(self, delimiter: str) -> None:
        """Open code at a backtick run, or close it at a run of the same length."""
        if not self._code_delimiter:
            self._code_delimiter = delimiter
        elif delimiter == self._code_delimiter:
            self._code_delimiter = ""

    def _renumber(self, marker: re.Match[str]) -> str:
        indexes = None if _is_subscript(marker) else self._indexes(marker.group(1))
        if indexes is None:
            return marker.group(0)
        numbers = [self._numbers.setdefault(index, len(self._numbers) + 1) for index in indexes]
        return "".join(f"[{number}]" for number in dict.fromkeys(numbers))

    def _indexes(self, items: str) -> list[int] | None:
        """Return the passage indexes a marker names, in order, or None when one names none."""
        indexes: list[int] = []
        for item in items.split(","):
            first, _, last = item.replace("–", "-").partition("-")
            low = int(first)
            high = int(last) if last.strip() else low
            if not (1 <= low <= high <= len(self._passages) and high - low < _MAX_RANGE):
                return None
            indexes.extend(range(low, high + 1))
        return indexes


def _is_subscript(marker: re.Match[str]) -> bool:
    """Whether a list or range in brackets is glued to the word before it, as in ``m[1, 2]``.

    A single number glued to a word, as in ``capital[1]``, is still a citation.
    """
    before = marker.string[marker.start() - 1 : marker.start()]
    glued = before.isalnum() or before == "_"
    return glued and _LIST_OR_RANGE.search(marker.group(1)) is not None


def citation_record(n: int, result: RetrievalResult) -> CitationRecord:
    """Describe one cited passage as the explorer ``Citation``."""
    metadata = result.metadata
    document_id = str(metadata.get("document_id", ""))
    locator = metadata.get("citation_locator")
    page = locator.get("page_start") if isinstance(locator, dict) else None
    return CitationRecord(
        n=n,
        chunk_id=result.id,
        document_id=document_id,
        title=display_text(metadata.get("document_title"), 256)
        or display_text(document_id, 128)
        or "Untitled document",
        snippet=snippet(result.text),
        url=citation_uri(locator),
        page=page if isinstance(page, int) and not isinstance(page, bool) else None,
    )


def snippet(text: str) -> str:
    """Collapse the passage's whitespace and cut it to ``SNIPPET_LIMIT`` characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= SNIPPET_LIMIT:
        return collapsed
    return collapsed[: SNIPPET_LIMIT - len(_ELLIPSIS)].rstrip() + _ELLIPSIS
