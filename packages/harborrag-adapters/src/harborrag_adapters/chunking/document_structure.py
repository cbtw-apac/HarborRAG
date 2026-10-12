"""Markdown-aware splitting inside one oversized unit.

Engine segmentation already splits a canonical document at its elements. Some
parsers (PDF layout engines, Confluence bodies) still hand over one element
that contains Markdown structure; this refiner splits such a unit at that
structure before falling back to prose rules:

0. ATX and setext headings (the cut goes before the heading),
1. fenced-code and block-quote edges,
2. list items and thematic breaks,
3. blank lines,
4+. the prose cuts of :mod:`text_structure`.

A fenced block is never cut except at its own line breaks, and only when the
fence alone is over the limit.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from harborrag_core.contracts.chunking import TextRefinementRequest

from ._base import BaseTextRefiner, Cut
from .text_structure import TEXT_PRIORITY_LEVELS, text_cuts

HEADING = 0
BLOCK_EDGE = 1
LIST_ITEM = 2
BLANK = 3
PROSE_BASE = 4
FENCE_LINE = PROSE_BASE + TEXT_PRIORITY_LEVELS

_ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)", re.MULTILINE)
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*\r?$", re.MULTILINE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})", re.MULTILINE)
_QUOTE_LINE = re.compile(r"^ {0,3}>", re.MULTILINE)
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\S", re.MULTILINE)
_THEMATIC_BREAK = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})\r?$", re.MULTILINE
)
_BLANK_LINES = re.compile(r"\n[ \t\r]*\n[ \t\r\n]*")


def looks_like_markdown(content: str) -> bool:
    """Report whether ``content`` carries Markdown block structure worth honouring."""

    if _ATX_HEADING.search(content) or _FENCE.search(content):
        return True
    return len(_LIST_ITEM.findall(content)) >= 2


def _line_start(content: str, offset: int) -> int:
    return content.rfind("\n", 0, offset) + 1


def _line_end(content: str, offset: int) -> int:
    newline = content.find("\n", offset)
    return len(content) if newline < 0 else newline + 1


def fenced_ranges(content: str) -> list[tuple[int, int]]:
    """Return ``[start, end)`` ranges of fenced code blocks, fences included."""

    ranges: list[tuple[int, int]] = []
    position = 0
    while True:
        opening = _FENCE.search(content, position)
        if opening is None:
            return ranges
        marker = opening.group(1)
        start = _line_start(content, opening.start())
        closing_pattern = re.compile(
            rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*\r?$", re.MULTILINE
        )
        closing = closing_pattern.search(content, _line_end(content, opening.start()))
        end = len(content) if closing is None else _line_end(content, closing.start())
        ranges.append((start, end))
        position = end


class DocumentStructureRefiner(BaseTextRefiner):
    """Split a Markdown-bearing unit at headings, blocks and list items first."""

    name = "document-structure"
    version = "1"

    def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
        content = request.content
        fences = fenced_ranges(content)
        cuts = [
            cut
            for cut in (
                *_heading_cuts(content),
                *_fence_cuts(content, fences),
                *self._quote_edges(content),
                *_list_cuts(content),
                *(Cut(match.end(), BLANK) for match in _BLANK_LINES.finditer(content)),
            )
            if not _strictly_inside(cut, fences)
        ]
        for start, end in _complement(fences, len(content)):
            cuts.extend(text_cuts(content, start, end, base=PROSE_BASE))
        return cuts

    @staticmethod
    def _quote_edges(content: str) -> list[Cut]:
        cuts: list[Cut] = []
        in_quote = False
        position = 0
        for line in content.splitlines(keepends=True):
            quoted = bool(_QUOTE_LINE.match(line))
            if quoted != in_quote:
                cuts.append(Cut(position, BLOCK_EDGE))
                in_quote = quoted
            position += len(line)
        return cuts


def _heading_cuts(content: str) -> list[Cut]:
    cuts = [
        Cut(_line_start(content, match.start()), HEADING)
        for match in _ATX_HEADING.finditer(content)
    ]
    for match in _SETEXT_UNDERLINE.finditer(content):
        underline = _line_start(content, match.start())
        if underline == 0:
            continue
        title = _line_start(content, underline - 1)
        if content[title:underline].strip():
            cuts.append(Cut(title, HEADING))
    return cuts


def _fence_cuts(content: str, fences: list[tuple[int, int]]) -> list[Cut]:
    cuts: list[Cut] = []
    for start, end in fences:
        cuts.extend((Cut(start, BLOCK_EDGE), Cut(end, BLOCK_EDGE)))
        cuts.extend(
            Cut(start + line.end(), FENCE_LINE) for line in re.finditer(r"\n", content[start:end])
        )
    return cuts


def _list_cuts(content: str) -> list[Cut]:
    return [
        Cut(_line_start(content, match.start()), LIST_ITEM)
        for pattern in (_LIST_ITEM, _THEMATIC_BREAK)
        for match in pattern.finditer(content)
    ]


def _strictly_inside(cut: Cut, fences: list[tuple[int, int]]) -> bool:
    if cut.priority == FENCE_LINE:
        return False
    return any(start < cut.offset < end for start, end in fences)


def _complement(ranges: list[tuple[int, int]], size: int) -> list[tuple[int, int]]:
    gaps: list[tuple[int, int]] = []
    position = 0
    for start, end in ranges:
        if start > position:
            gaps.append((position, start))
        position = max(position, end)
    if position < size:
        gaps.append((position, size))
    return gaps
