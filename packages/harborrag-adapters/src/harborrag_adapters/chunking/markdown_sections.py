"""Section-first chunking of a whole Markdown document.

The refiners in this package split one oversized unit; this module is the step
in front of them for a complete Markdown file:

1. split the file into sections at ATX headings (outside fenced code), each
   with the heading path above it. Numbered headings ("4.3.1 ...") nest by
   their numbering, so inconsistent ``#`` levels still give the right path;
2. give every paragraph a role. Body text is what you retrieve; the title
   block (authors, dates), contact and affiliation lines, back matter
   (Declarations, Acknowledgements, Funding, ...) and the reference list are
   tagged so a caller can leave them out;
3. join a section below ``minimum_tokens`` only to a related neighbour: a
   bare heading or short intro always leads into its first child or next
   sibling, and short siblings share a chunk only up to half the maximum, so
   unrelated sections never mix and related ones stay focused;
4. refine each group with the routing refiner under the budget left after its
   context header (document title and heading path). A header longer than
   half the maximum is shortened -- title first, then outer headings -- and
   each chunk's own header is shortened the same way until it fits;
5. fold a split still below ``minimum_tokens`` (a figure caption, a section
   tail) into a neighbour, or, when the two do not fit together, re-cut the
   pair at the paragraph or sentence end that evens them out.

A chunk is labelled with the deepest heading path it stays inside (a parent
intro plus its child is labelled with the child), lists every section it
touches in ``sections``, and names joined sibling sections in its header.

Every chunk's ``content`` is exactly ``text[start_offset:end_offset]``;
``embed_text`` is the header plus the content, and fits ``maximum_tokens``.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextRefiner,
    TokenCounter,
)
from harborrag_core.ingestion import ChunkValidationError

from .document_structure import fenced_ranges, is_bare_heading
from .factory import build_text_refiner
from .text_structure import LINE, text_cuts


class ChunkRole(StrEnum):
    """What part of the document a chunk comes from."""

    BODY = "body"
    FRONT_MATTER = "front_matter"
    BACK_MATTER = "back_matter"
    REFERENCES = "references"


@dataclass(frozen=True, slots=True)
class MarkdownChunk:
    content: str
    token_count: int
    source_span: SourceSpan
    role: ChunkRole
    structural_path: tuple[str, ...]
    sections: tuple[tuple[str, ...], ...]
    context: str
    embed_text: str
    embed_token_count: int
    boundary_kind: SplitBoundaryKind
    forced_split: bool


_CONTEXT_SEPARATOR = " › "
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*\r?$", re.MULTILINE)
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?(?:\s|$)")
_BLANK_LINES = re.compile(r"\n[ \t\r]*\n[ \t\r\n]*")

_REFERENCE_TITLE = re.compile(r"(?i)references|bibliography|works cited|literature cited")
_BACK_MATTER_TITLE = re.compile(
    r"(?i)acknowledge?ments?|declarations?|funding|conflicts? of interests?|competing interests?"
    r"|open access|author contributions?|data (?:and code )?availability|code availability"
    r"|ethics(?: approval| declarations?)?|consent.*|author information|additional information"
)
# A paragraph opening with a back-matter label: "Acknowledgements This work ...".
_BACK_MATTER_LABEL = re.compile(
    r"^[*_]*(?i:acknowledge?ments?|author contributions?|funding|data (?:and code )?availability"
    r"|code availability|competing interests?|conflicts? of interests?|ethics (?:approval|declarations?)"
    r"|consent (?:to|for) [a-z ]+)[*_:.]*(?:\s+[A-Z(]|\s*$)"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_AFFILIATION = re.compile(r"^\s*(?:[¹²³⁴⁵⁶⁷⁸⁹⁰]+|<sup>\d+</sup>)\s*\S")
_EQUAL_CONTRIBUTION = re.compile(r"(?i)contributed equally|equal contribution")


@dataclass(frozen=True, slots=True)
class _Heading:
    title: str
    level: int
    depth: int | None


@dataclass(frozen=True, slots=True)
class _Section:
    start: int
    end: int
    path: tuple[str, ...]
    role: ChunkRole


@dataclass(slots=True)
class _Group:
    """Contiguous text of one role that is refined as one request."""

    start: int
    end: int
    role: ChunkRole
    paths: list[tuple[str, ...]]
    tokens: int


class MarkdownChunker:
    """Chunk a Markdown document section by section."""

    name = "markdown-sections"
    version = "3"

    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        maximum_tokens: int = 512,
        overlap_tokens: int = 0,
        minimum_tokens: int | None = None,
        refiner: TextRefiner | None = None,
    ) -> None:
        if maximum_tokens < 1:
            raise ValueError("maximum_tokens must be positive")
        if not 0 <= overlap_tokens < maximum_tokens:
            raise ValueError("overlap_tokens must satisfy 0 <= overlap < maximum")
        minimum = maximum_tokens // 4 if minimum_tokens is None else minimum_tokens
        if not 0 <= minimum <= maximum_tokens:
            raise ValueError("minimum_tokens must satisfy 0 <= minimum <= maximum")
        self._counter = token_counter
        self._maximum = maximum_tokens
        self._overlap = overlap_tokens
        self._minimum = minimum
        self._refiner = refiner or build_text_refiner(token_counter)

    def fingerprint(self) -> str:
        """Identify the boundary behaviour for processing-identity hashes."""

        refiner = getattr(self._refiner, "fingerprint", None)
        inner = refiner() if callable(refiner) else type(self._refiner).__name__
        return f"{self.name}:{self.version}({inner})"

    def chunk(self, text: str, *, title: str | None = None) -> tuple[MarkdownChunk, ...]:
        """Return the chunks of ``text`` in document order, every role included.

        ``title`` overrides the document title detected from a leading ``#``
        heading; it opens every chunk's context header.
        """

        sections, detected = _sections(text)
        document = _Document(text, sections, title or detected)
        chunks: list[MarkdownChunk] = []
        for group in self._groups(document):
            chunks.extend(self._chunk_group(document, group))
        return tuple(chunks)

    def _count(self, text: str) -> int:
        return self._counter.count(text)

    def _groups(self, document: _Document) -> list[_Group]:
        groups: list[_Group] = []
        for section in document.sections:
            for start, end, role in _role_runs(document.text, section, document.fences):
                tokens = self._count(document.text[start:end])
                last = groups[-1] if groups else None
                if last is not None and self._joins(
                    document.text, last, start, end, role, section.path, tokens
                ):
                    last.end = end
                    last.paths.append(section.path)
                    last.tokens = self._count(document.text[last.start : end])
                else:
                    groups.append(_Group(start, end, role, [section.path], tokens))
        # A heading with nothing after it (the last line of the file) is not a chunk.
        return [
            group
            for group in groups
            if document.text[group.start : group.end].strip()
            and not is_bare_heading(document.text[group.start : group.end])
        ]

    def _joins(
        self,
        text: str,
        group: _Group,
        start: int,
        end: int,
        role: ChunkRole,
        path: tuple[str, ...],
        tokens: int,
    ) -> bool:
        if group.role is not role or group.end != start:
            return False
        if role is not ChunkRole.BODY:
            return True
        if not _related(group.paths[-1], path):
            return False
        # A heading or short intro always leads into what follows; a short
        # section only joins siblings while the group stays a focused size.
        # A bare heading never trails a group that already has text.
        if is_bare_heading(text[start:end]) and not is_bare_heading(text[group.start : group.end]):
            return False
        if group.tokens < self._minimum:
            return True
        return tokens < self._minimum and self._count(text[group.start : end]) <= self._maximum // 2

    def _chunk_group(self, document: _Document, group: _Group) -> list[MarkdownChunk]:
        text = document.text
        # A chunk's header names titles from its group's paths, so a header
        # naming every distinct title in the group bounds it.
        titles = tuple(dict.fromkeys(title for path in group.paths for title in path))
        # The header may take only what leaves the content a workable budget
        # (half the maximum, and room for the overlap); a longer one is shortened.
        content_floor = max(self._overlap + 1, self._maximum // 2)
        bound = self._fit_context(document.context_parts(titles), self._maximum - content_floor)
        header = self._count(bound + "\n\n") if bound else 0
        limit = self._maximum - header
        request = TextRefinementRequest(
            content=text[group.start : group.end],
            maximum_tokens=limit,
            overlap_tokens=self._overlap,
            source_span=document.span(group.start, group.end),
            boundary_kind=SplitBoundaryKind.SECTION,
            structural_path=_common_prefix(group.paths),
        )
        positioned: list[tuple[int, int, bool]] = []
        for split in self._refiner.split(request):
            span = split.source_span
            if span is None or span.start_offset is None or span.end_offset is None:
                continue
            positioned.append((span.start_offset, span.end_offset, split.forced_split))
        windowed = [(start, end) for start, end, forced in positioned if forced]
        ranges = self._settle_small(text, [(start, end) for start, end, _ in positioned], limit)

        chunks: list[MarkdownChunk] = []
        for start, end in ranges:
            # Forced only if a token-window cut (not a paragraph or sentence end) shaped it.
            forced = any(low < end and start < high for low, high in windowed)
            kind = SplitBoundaryKind.SECTION if len(ranges) == 1 else SplitBoundaryKind.PARAGRAPH
            content = text[start:end]
            if not content.strip():
                continue
            sections, labelled = document.sections_between(start, end)
            path, branches = _descend(labelled)
            context, embed_text = self._embed(document.context_parts(path, branches), content)
            chunks.append(
                MarkdownChunk(
                    content=content,
                    token_count=self._count(content),
                    source_span=document.span(start, end),
                    role=group.role,
                    structural_path=path,
                    sections=tuple(sections),
                    context=context,
                    embed_text=embed_text,
                    embed_token_count=self._count(embed_text),
                    boundary_kind=SplitBoundaryKind.FORCED if forced else kind,
                    forced_split=forced,
                )
            )
        return chunks

    def _fit_context(self, parts: Sequence[str], budget: int) -> str:
        """Longest suffix of ``parts`` whose header (context plus blank line) fits ``budget``.

        Outer parts go first -- the document title, then outer headings -- so
        the most specific heading is the last to be dropped.
        """

        for start in range(len(parts)):
            context = _CONTEXT_SEPARATOR.join(parts[start:])
            if self._count(context + "\n\n") <= budget:
                return context
        return ""

    def _embed(self, parts: Sequence[str], content: str) -> tuple[str, str]:
        """Return the context and embed text, shortening the context until both fit."""

        body = content.strip()
        for start in range(len(parts) + 1):
            context = _CONTEXT_SEPARATOR.join(parts[start:])
            embed_text = f"{context}\n\n{body}" if context else body
            if self._count(embed_text) <= self._maximum:
                return context, embed_text
        raise ChunkValidationError(
            f"{self.name} produced content above maximum_tokens={self._maximum} "
            "even without a context header"
        )

    def _settle_small(
        self, text: str, ranges: list[tuple[int, int]], limit: int
    ) -> list[tuple[int, int]]:
        """Merge or rebalance splits below ``minimum_tokens`` with a neighbour."""

        settled = list(ranges)
        kept_small: set[tuple[int, int]] = set()
        while len(settled) > 1:
            small = next(
                (
                    position
                    for position, (start, end) in enumerate(settled)
                    if (start, end) not in kept_small
                    and self._count(text[start:end]) < self._minimum
                ),
                None,
            )
            if small is None:
                break
            merges = []
            for neighbour in (small - 1, small + 1):
                if 0 <= neighbour < len(settled):
                    low, high = sorted((neighbour, small))
                    start, end = settled[low][0], settled[high][1]
                    tokens = self._count(text[start:end])
                    if tokens <= limit:
                        merges.append((tokens, low, start, end))
            if merges:
                _, low, start, end = min(merges)
                settled[low : low + 2] = [(start, end)]
                continue
            small_tokens = self._count(text[settled[small][0] : settled[small][1]])
            for neighbour in (small - 1, small + 1):
                if 0 <= neighbour < len(settled):
                    low, high = sorted((neighbour, small))
                    pair = self._rebalance(
                        text, settled[low][0], settled[high][1], limit, small_tokens
                    )
                    if pair is not None:
                        settled[low : low + 2] = pair
                        break
            else:
                kept_small.add(settled[small])
        return settled

    def _rebalance(
        self,
        text: str,
        start: int,
        end: int,
        limit: int,
        small_tokens: int,
    ) -> list[tuple[int, int]] | None:
        """Re-cut ``text[start:end]`` in two at the boundary that evens them out.

        Only paragraph, sentence and line ends outside fenced code qualify.
        Among cuts within 80% of the most even one, the strongest boundary
        wins. The pair is returned only if its smaller side beats
        ``small_tokens``; the second split then starts up to ``overlap_tokens``
        earlier, at a sentence end.
        """

        content = text[start:end]
        fences = fenced_ranges(content)
        cuts: dict[int, int] = {}
        for cut in text_cuts(content):
            if cut.priority > LINE or not 0 < cut.offset < len(content):
                continue
            if any(fence_start < cut.offset < fence_end for fence_start, fence_end in fences):
                continue
            cuts[cut.offset] = min(cut.priority, cuts.get(cut.offset, cut.priority))
        candidates: list[tuple[int, int, int]] = []
        for offset, priority in cuts.items():
            left, right = self._count(content[:offset]), self._count(content[offset:])
            if left <= limit and right <= limit:
                candidates.append((min(left, right), priority, offset))
        if not candidates:
            return None
        best = max(even for even, _, _ in candidates)
        even, _, offset = min(
            (candidate for candidate in candidates if candidate[0] >= 0.8 * best),
            key=lambda candidate: (candidate[1], -candidate[0]),
        )
        if even <= small_tokens:
            return None

        second = offset
        if self._overlap:
            for position in sorted(cuts, reverse=True):
                if position >= offset:
                    continue
                if self._count(content[position:offset]) > self._overlap:
                    break
                if self._count(content[position:]) <= limit:
                    second = position
        return [(start, start + offset), (start + second, end)]


class _Document:
    """The text plus its section index, line index and title."""

    def __init__(self, text: str, sections: list[_Section], title: str | None) -> None:
        self.text = text
        self.sections = sections
        self.title = title
        self.fences = fenced_ranges(text)
        self._section_starts = [section.start for section in sections]
        self._line_starts = [0] + [match.end() for match in re.finditer(r"\n", text)]

    def span(self, start: int, end: int) -> SourceSpan:
        return SourceSpan(
            start_offset=start,
            end_offset=end,
            start_line=bisect_right(self._line_starts, start),
            end_line=bisect_right(self._line_starts, max(end - 1, start)),
        )

    def sections_between(
        self, start: int, end: int
    ) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
        """Heading paths of the sections ``[start, end)`` touches, in order.

        Returns every touched path, and the ones that label the chunk: sections
        that contribute only their heading (a parent directly followed by a
        child) are left out of the label unless nothing else is there.
        """

        first = max(bisect_right(self._section_starts, start) - 1, 0)
        touched: list[tuple[str, ...]] = []
        with_text: list[tuple[str, ...]] = []
        for section in self.sections[first:]:
            if section.start >= end:
                break
            piece = self.text[max(section.start, start) : min(section.end, end)]
            if piece.strip():
                touched.append(section.path)
                if not is_bare_heading(piece):
                    with_text.append(section.path)
        return touched or [()], with_text or touched or [()]

    def context_parts(self, path: tuple[str, ...], branches: Sequence[str] = ()) -> list[str]:
        """``[title, *path]``, then ``a | b`` for sibling sections sharing the chunk."""

        parts = [part for part in (self.title, *path) if part]
        if branches:
            parts.append(" | ".join(branches))
        return parts


def _sections(text: str) -> tuple[list[_Section], str | None]:
    """Split ``text`` at ATX headings; return the sections and the document title.

    A leading, unnumbered ``#`` heading followed by other headings is the
    document title: its own block (authors, dates) is front matter and it is
    left out of every heading path.
    """

    fences = fenced_ranges(text)
    headings = [
        (match.start(), len(match.group(1)), match.group(2).strip().strip("*_").strip())
        for match in _ATX.finditer(text)
        if not any(start <= match.start() < end for start, end in fences)
    ]
    headings = [(offset, level, title) for offset, level, title in headings if title]

    title: str | None = None
    if (
        len(headings) > 1
        and headings[0][1] == 1
        and not _NUMBERED.match(headings[0][2])
        and not text[: headings[0][0]].strip()
    ):
        title = headings[0][2]

    sections: list[_Section] = []
    if not headings or headings[0][0] > 0:
        sections.append(_Section(0, headings[0][0] if headings else len(text), (), ChunkRole.BODY))

    stack: list[_Heading] = []
    for position, (offset, level, heading_title) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(text)
        if position == 0 and title is not None:
            sections.append(_Section(offset, end, (), ChunkRole.FRONT_MATTER))
            continue
        numbered = _NUMBERED.match(heading_title)
        heading = _Heading(
            heading_title, level, len(numbered.group(1).split(".")) if numbered else None
        )
        while stack and _closes(stack[-1], heading):
            stack.pop()
        stack.append(heading)
        path = tuple(entry.title for entry in stack)
        sections.append(_Section(offset, end, path, _section_role(path)))
    return sections, title


def _closes(open_heading: _Heading, heading: _Heading) -> bool:
    """Report whether ``heading`` ends the section of ``open_heading``."""

    if open_heading.depth is not None and heading.depth is not None:
        return open_heading.depth >= heading.depth
    return open_heading.level >= heading.level


def _section_role(path: tuple[str, ...]) -> ChunkRole:
    titles = [_NUMBERED.sub("", title).strip() for title in path]
    if any(_REFERENCE_TITLE.fullmatch(title) for title in titles):
        return ChunkRole.REFERENCES
    if any(_BACK_MATTER_TITLE.fullmatch(title) for title in titles):
        return ChunkRole.BACK_MATTER
    return ChunkRole.BODY


def _role_runs(
    text: str,
    section: _Section,
    fences: list[tuple[int, int]],
) -> list[tuple[int, int, ChunkRole]]:
    """Split a body section into runs of paragraphs that share a role."""

    if section.role is not ChunkRole.BODY:
        return [(section.start, section.end, section.role)]
    bounds = [section.start]
    for match in _BLANK_LINES.finditer(text, section.start, section.end):
        if match.end() < section.end and not any(
            start < match.end() < end for start, end in fences
        ):
            bounds.append(match.end())
    bounds.append(section.end)

    runs: list[tuple[int, int, ChunkRole]] = []
    for start, end in zip(bounds, bounds[1:], strict=False):
        role = _paragraph_role(text[start:end])
        if runs and runs[-1][2] is role:
            runs[-1] = (runs[-1][0], end, role)
        else:
            runs.append((start, end, role))
    return runs


def _paragraph_role(paragraph: str) -> ChunkRole:
    stripped = paragraph.strip()
    if _BACK_MATTER_LABEL.match(stripped):
        return ChunkRole.BACK_MATTER
    lines = [line for line in stripped.splitlines() if line.strip()]
    if lines and (
        2 * sum(bool(_EMAIL.search(line)) for line in lines) >= len(lines)
        or all(_AFFILIATION.match(line) for line in lines)
    ):
        return ChunkRole.FRONT_MATTER
    if len(stripped) < 300 and _EQUAL_CONTRIBUTION.search(stripped):
        return ChunkRole.FRONT_MATTER
    return ChunkRole.BODY


def _related(previous: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """Siblings, or a parent followed by its child, may share a chunk."""

    return path[:-1] == previous[:-1] or path[:-1] == previous


def _descend(paths: Sequence[tuple[str, ...]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the deepest path the chunk stays inside, and the branches below it.

    Starting from the shared prefix, the path follows the headings down while
    only one way leads deeper: a parent's intro followed by its child, then
    the grandchild, gives the grandchild's full path. Where sibling sections
    share the chunk, their titles are returned as branches.
    """

    path = _common_prefix(paths)
    while True:
        deeper = list(
            dict.fromkeys(
                candidate[len(path)]
                for candidate in paths
                if len(candidate) > len(path) and candidate[: len(path)] == path
            )
        )
        if len(deeper) != 1:
            return path, tuple(deeper)
        path = (*path, deeper[0])


def _common_prefix(paths: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    if not paths:
        return ()
    shared = paths[0]
    for path in paths[1:]:
        length = 0
        while length < min(len(shared), len(path)) and shared[length] == path[length]:
            length += 1
        shared = shared[:length]
    return tuple(shared)
