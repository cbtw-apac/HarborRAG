"""Number an answer's citations by first appearance and describe each cited passage.

The model cites passage ``k`` of its prompt as ``[k]``, or several as ``[k, m]`` or
``[k-m]``. The caller sees ``[n]``, where ``n`` counts the passages in the order the
answer first cites them, one number per bracket, so every ``[n]`` in the answer has the
citation ``n`` and every citation is cited. A number naming no passage is dropped from
the text, with the space before it, rather than left without a citation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from harborrag_core.domain.retrieval import RetrievalResult

from ..memory import CitationRecord
from .presenters import citation_uri, display_text

SNIPPET_LIMIT = 300
_MAX_RANGE = 20
_ITEM = r"\d{1,4}(?:\s*[-–]\s*\d{1,4})?"
_MARKER = re.compile(rf"(\s*)\[\s*({_ITEM}(?:\s*,\s*{_ITEM})*)\s*\]")
_HELD = re.compile(r"(?:\s*\[[\d\s,\-–]{0,40}|\s+)$")
_ELLIPSIS = "…"


class CitationNumbering:
    """Rewrite one answer's markers as it arrives, in one piece or many.

    ``feed`` returns the text that can be shown so far; a marker split across pieces,
    and the whitespace before it, is held back until it closes. ``finish`` returns what
    is left. The concatenated output is the answer as stored and returned.
    """

    def __init__(self, passages: Sequence[RetrievalResult]) -> None:
        self._passages = tuple(passages)
        self._numbers: dict[int, int] = {}
        self._pending = ""

    def feed(self, text: str) -> str:
        """Return the renumbered text that no later piece can change."""
        buffered = self._pending + text
        held = _HELD.search(buffered)
        cut = held.start() if held else len(buffered)
        self._pending = buffered[cut:]
        return _MARKER.sub(self._renumber, buffered[:cut])

    def finish(self) -> str:
        """Return the held-back rest of the answer."""
        rest, self._pending = self._pending, ""
        return _MARKER.sub(self._renumber, rest)

    def citations(self) -> tuple[CitationRecord, ...]:
        """The cited passages, ``n`` ascending."""
        order = sorted(self._numbers.items(), key=lambda item: item[1])
        return tuple(citation_record(n, self._passages[index - 1]) for index, n in order)

    def _renumber(self, marker: re.Match[str]) -> str:
        numbers = [
            self._numbers.setdefault(index, len(self._numbers) + 1)
            for index in self._indexes(marker.group(2))
        ]
        if not numbers:
            return ""
        return marker.group(1) + "".join(f"[{number}]" for number in dict.fromkeys(numbers))

    def _indexes(self, items: str) -> list[int]:
        """The passage indexes a marker names, in order; unknown ones left out."""
        indexes: list[int] = []
        for item in items.split(","):
            first, _, last = item.replace("–", "-").partition("-")
            low = int(first)
            high = int(last) if last.strip() else low
            if 1 <= low <= high <= len(self._passages) and high - low < _MAX_RANGE:
                indexes.extend(range(low, high + 1))
        return indexes


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
    """The passage with whitespace collapsed, cut to ``SNIPPET_LIMIT`` characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= SNIPPET_LIMIT:
        return collapsed
    return collapsed[: SNIPPET_LIMIT - len(_ELLIPSIS)].rstrip() + _ELLIPSIS
