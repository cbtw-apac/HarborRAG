"""Link extraction from Markdown, in one linear left-to-right scan.

Finds inline links, images and ``<scheme://...>`` autolinks, skipping fenced
blocks and code spans. There is no backtracking regex: every scan is bounded
by a per-link length cap and a shared work budget, so hostile input costs
bounded time instead of quadratic time.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

_MAX_PART = 2000  # longest link text or target we will read
_WORK_BUDGET = 2_000_000  # characters spent on scans that did not yield a link
_FENCE = re.compile(r"[ ]{0,3}(`{3,}|~{3,})")
_BACKTICKS = re.compile(r"`+")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}://")
_BLANK_LINE = re.compile(r"\n(?=[ \t]*\n)")  # the first newline of a blank line


@dataclass(frozen=True)
class Link:
    target: str
    text: str
    image: bool


def _strip_fences(markdown: str) -> str:
    """Blank out fenced code blocks, keeping the other lines untouched."""
    kept: list[str] = []
    fence = ""
    for line in markdown.splitlines(keepends=True):
        if fence:
            match = _FENCE.match(line)
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                and not line[match.end() :].strip()
            ):
                fence = ""
            continue
        opener = _FENCE.match(line)
        if opener and not (opener.group(1)[0] == "`" and "`" in line[opener.end() :]):
            fence = opener.group(1)
            continue
        kept.append(line)
    return "".join(kept)


def _breaks(text: str) -> list[int]:
    """Where each blank line (only spaces or tabs) starts, found in one linear pass."""
    return [match.start() for match in _BLANK_LINE.finditer(text)]


def _has_break(breaks: list[int], start: int, stop: int) -> bool:
    """Whether a blank line starts in ``text[start:stop]``, a paragraph break."""
    at = bisect_left(breaks, start)
    return at < len(breaks) and breaks[at] < stop


def extract_links(markdown: str, *, limit: int = 1000) -> tuple[Link, ...]:
    """Return up to ``limit`` distinct links in document order (first target wins)."""
    text = _strip_fences(markdown.replace("\r\n", "\n").replace("\r", "\n"))
    runs: dict[int, list[int]] = {}
    for run in _BACKTICKS.finditer(text):
        runs.setdefault(run.end() - run.start(), []).append(run.start())
    breaks = _breaks(text)
    found: dict[str, Link] = {}
    budget = _WORK_BUDGET
    size = len(text)
    escaped = -1  # index of the last character a backslash escaped
    i = 0
    while i < size and len(found) < limit:
        char = text[i]
        if char == "\\":
            escaped = i + 1
            i += 2
        elif char == "`":
            i = _skip_code_span(text, i, runs, breaks)
        elif char == "<":
            target = _autolink(text, i)
            if target:
                found.setdefault(target, Link(target, target, False))
            i += 1
        elif char == "[" and budget > 0:
            image = i > 0 and text[i - 1] == "!" and escaped != i - 1
            parsed, spent = _inline_link(text, i, breaks)
            budget -= spent
            if parsed:
                target, label = parsed
                found.setdefault(target, Link(target, label, image))
            i += 1
        else:
            i += 1
    return tuple(found.values())


def _skip_code_span(text: str, i: int, runs: dict[int, list[int]], breaks: list[int]) -> int:
    """Index after the code span opened at ``i`` (or after the lone backtick run).

    An escaped backtick can leave the opener shorter than the run indexed in
    ``runs``; closers are whole runs, since escapes do not apply inside a span.
    """
    end = i
    while end < len(text) and text[end] == "`":
        end += 1
    starts = runs.get(end - i, ())
    at = bisect_right(starts, i)
    if at < len(starts) and not _has_break(breaks, i, starts[at]):
        return starts[at] + (end - i)  # a code span stays inside one paragraph
    return end


def _autolink(text: str, start: int) -> str:
    scheme = _SCHEME.match(text, start + 1)
    if not scheme:
        return ""
    stop = min(len(text), start + 1 + _MAX_PART)
    end = scheme.end()
    while end < stop and text[end] not in "> \t\r\n<":
        end += 1
    if end >= len(text) or text[end] != ">" or end == scheme.end():
        return ""
    return text[start + 1 : end]


def _inline_link(text: str, start: int, breaks: list[int]) -> tuple[tuple[str, str] | None, int]:
    """Parse ``[label](target)`` at ``start``; return the result and chars scanned."""
    size = len(text)
    stop = min(size, start + 1 + _MAX_PART)
    depth = 1
    j = start + 1
    while j < stop:
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth or j >= stop or text[j + 1 : j + 2] != "(":
        return None, j - start
    label = text[start + 1 : j].strip()
    target, end = _target(text, j + 2, breaks)
    if not target:
        return None, end - start
    return (target, label), 0


def _target(text: str, start: int, breaks: list[int]) -> tuple[str, int]:
    """Read a link target after ``(``: ``<...>`` or balanced, no whitespace."""
    stop = min(len(text), start + _MAX_PART)
    if text[start : start + 1] == "<":
        end = start + 1
        while end < stop and text[end] not in ">\r\n":
            end += 1
        if end < stop and text[end] == ">" and text.find(")", end + 1, stop) != -1:
            return text[start + 1 : end], end
        return "", end
    return _plain_target(text, start, stop, breaks)


def _plain_target(text: str, start: int, stop: int, breaks: list[int]) -> tuple[str, int]:
    depth = 1
    j = start
    while j < stop:
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:j], j
        elif char in " \t\r\n":
            close = text.find(")", j, stop) if depth == 1 else -1
            if close != -1 and not _has_break(breaks, j, close):
                return text[start:j], close  # optional title after the target
            return "", j
        j += 1
    return "", j
