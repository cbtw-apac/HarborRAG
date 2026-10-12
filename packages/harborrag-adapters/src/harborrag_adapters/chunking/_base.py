"""Template for structure-aware text refiners.

A strategy only answers one question -- where may this text be cut, and how
strong is each cut -- by returning :class:`Cut` values from ``_propose``. The
base turns those into splits the same way for every strategy:

1. split the text at its strongest cuts, recursing into any piece that is still
   over the limit with the next-strongest cuts (structure first);
2. merge neighbouring pieces that came from the same parent back together while
   they fit (so a split never leaves needlessly small fragments);
3. force-split any piece that has no cuts left with a token-bounded window;
4. recount every split with the injected counter, apply overlap, check that the
   splits tile the source, and map them to source spans.

Library token counts are never trusted: engine rejects any split above the
limit under its own counter, so the counter injected here is the only judge.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar

from harborrag_core.contracts.chunking import (
    SplitBoundaryKind,
    TextRefinementRequest,
    TextSplit,
    TokenCounter,
)
from harborrag_core.ingestion import ChunkValidationError

from ._spans import source_span, validate_coverage


@dataclass(frozen=True, slots=True)
class Cut:
    """A position where ``request.content`` may be split.

    ``offset`` is the index of the first character of the following piece.
    Lower ``priority`` is a stronger boundary. ``scope`` describes the piece that
    starts at this offset (code refiners record enclosing definitions here).
    """

    offset: int
    priority: int
    scope: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CutIndex:
    """Cuts sorted by offset, with their offsets for range lookups."""

    cuts: list[Cut]
    offsets: list[int] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "offsets", [cut.offset for cut in self.cuts])

    def inside(self, start: int, end: int) -> list[Cut]:
        return self.cuts[bisect_right(self.offsets, start) : bisect_left(self.offsets, end)]


@dataclass(frozen=True, slots=True)
class _Piece:
    start: int
    end: int
    scope: tuple[str, ...]
    tokens: int


class BaseTextRefiner(ABC):
    """Implement core ``TextRefiner`` for strategies that propose cut positions."""

    name: ClassVar[str]
    version: ClassVar[str]

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def fingerprint(self) -> str:
        """Identify the boundary behaviour for processing-identity hashes."""

        return f"{self.name}:{self.version}"

    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        """Return ordered splits that each fit ``request.maximum_tokens``."""

        content = request.content
        if not content.strip():
            return ()
        total = self._count(content)
        if total < 1:
            return ()
        if total <= request.maximum_tokens:
            return (
                TextSplit(
                    content=content,
                    token_count=total,
                    source_span=request.source_span,
                    boundary_kind=request.boundary_kind,
                    structural_path=request.structural_path,
                ),
            )

        index = _CutIndex(self._normalized_cuts(self._propose(request), len(content)))
        # Pack below the limit by the overlap so each split has room to repeat
        # the end of the previous one without breaking the limit.
        packing = _Packing(
            content,
            max(request.maximum_tokens - request.overlap_tokens, 1),
            index,
            self,
        )
        pieces = packing.descend(_Piece(0, len(content), (), total))
        ranges = self._with_overlap(request, pieces, index.offsets)
        validate_coverage([(start, end) for start, end, _ in ranges], len(content))

        results: list[TextSplit] = []
        for start, end, scope in ranges:
            text = content[start:end]
            if not text.strip():
                continue
            count = self._count(text)
            if count < 1:
                continue
            if count > request.maximum_tokens:
                raise ChunkValidationError(
                    f"{self.name} produced a split above maximum_tokens ({count})"
                )
            results.append(
                TextSplit(
                    content=text,
                    token_count=count,
                    source_span=source_span(request, start, end),
                    boundary_kind=SplitBoundaryKind.FORCED,
                    structural_path=request.structural_path,
                    prefix="\n".join(scope) if scope else None,
                    forced_split=True,
                )
            )
        if not results:
            raise ChunkValidationError(f"{self.name} returned no content")
        return tuple(results)

    @abstractmethod
    def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
        """Return every position where the request's content may be cut."""

    def _count(self, text: str) -> int:
        return self._token_counter.count(text)

    @staticmethod
    def _normalized_cuts(cuts: Sequence[Cut], size: int) -> list[Cut]:
        """Keep one cut per offset (the strongest), strictly inside the text."""

        best: dict[int, Cut] = {}
        for cut in cuts:
            if not 0 < cut.offset < size:
                continue
            current = best.get(cut.offset)
            if current is None or cut.priority < current.priority:
                best[cut.offset] = cut
        return [best[offset] for offset in sorted(best)]

    def _with_overlap(
        self,
        request: TextRefinementRequest,
        pieces: list[_Piece],
        offsets: list[int],
    ) -> list[tuple[int, int, tuple[str, ...]]]:
        """Extend each split backwards over at most ``overlap_tokens`` of context.

        Overlap starts only at a proposed cut so it never begins mid-word, and is
        skipped whenever it would break the limit. Callers that must not repeat
        content (line-preserving roles) ask for zero overlap.
        """

        overlap = request.overlap_tokens
        if overlap < 1:
            return [(piece.start, piece.end, piece.scope) for piece in pieces]
        content = request.content
        ranges: list[tuple[int, int, tuple[str, ...]]] = []
        previous_start = 0
        for position, piece in enumerate(pieces):
            candidates: list[int] = []
            if position:
                lower = bisect_right(offsets, previous_start)
                upper = bisect_left(offsets, piece.start)
                for candidate in reversed(offsets[lower:upper]):
                    if self._count(content[candidate : piece.start]) > overlap:
                        break
                    candidates.append(candidate)
            start = piece.start
            # Furthest-back first; step forward only if the limit is broken.
            for candidate in reversed(candidates):
                if self._count(content[candidate : piece.end]) <= request.maximum_tokens:
                    start = candidate
                    break
            ranges.append((start, piece.end, piece.scope))
            previous_start = piece.start
        return ranges


class _Packing:
    """Structure-first descent, sibling merging and windowing for one request."""

    def __init__(
        self,
        content: str,
        limit: int,
        index: _CutIndex,
        refiner: BaseTextRefiner,
    ) -> None:
        self._content = content
        self._limit = limit
        self._index = index
        self._refiner = refiner

    def _count(self, start: int, end: int) -> int:
        return self._refiner._count(self._content[start:end])

    def descend(self, span: _Piece) -> list[_Piece]:
        if span.tokens <= self._limit:
            return [span]

        inside = self._index.inside(span.start, span.end)
        if not inside:
            return self._window(span)

        strongest = min(cut.priority for cut in inside)
        bounds = [(span.start, span.scope)] + [
            (cut.offset, cut.scope) for cut in inside if cut.priority == strongest
        ]
        children: list[list[_Piece]] = []
        for position, (child_start, child_scope) in enumerate(bounds):
            child_end = bounds[position + 1][0] if position + 1 < len(bounds) else span.end
            child = _Piece(child_start, child_end, child_scope, self._count(child_start, child_end))
            children.append(self.descend(child))
        return self._merge_siblings(children)

    def _merge_siblings(self, children: list[list[_Piece]]) -> list[_Piece]:
        """Greedily rejoin whole siblings while they fit.

        A child that itself had to be split stays as its own run of pieces; the
        only merge into it is a small preceding sibling joining its first piece,
        which removes a cut without creating a new one. Its tail is never
        extended into the next sibling, so the end of one definition is not
        glued onto an unrelated one.

        Merged sizes are estimated by adding piece counts, and every merged
        piece is then counted once for real. A counter that is not additive
        (a BPE merge across the join) falls back to exact counting for that run.
        """

        merged = self._merge(children, exact=False)
        if all(piece.tokens <= self._limit for piece in merged):
            return merged
        return self._merge(children, exact=True)

    def _merge(self, children: list[list[_Piece]], *, exact: bool) -> list[_Piece]:
        merged: list[_Piece] = []
        open_piece: _Piece | None = None
        for child in children:
            if len(child) == 1:
                piece = child[0]
                joined = self._join(open_piece, piece, exact=exact) if open_piece else None
                if joined is None:
                    if open_piece is not None:
                        merged.append(self._settle(open_piece))
                    open_piece = piece
                else:
                    open_piece = joined
                continue
            first, *rest = child
            if open_piece is not None:
                joined = self._join(open_piece, first, exact=exact)
                if joined is None:
                    merged.append(self._settle(open_piece))
                else:
                    first = joined
                open_piece = None
            merged.extend((self._settle(first), *rest))
        if open_piece is not None:
            merged.append(self._settle(open_piece))
        return merged

    def _join(self, left: _Piece, right: _Piece, *, exact: bool) -> _Piece | None:
        tokens = self._count(left.start, right.end) if exact else left.tokens + right.tokens
        if tokens > self._limit:
            return None
        return _Piece(left.start, right.end, _common_prefix(left.scope, right.scope), tokens)

    def _settle(self, piece: _Piece) -> _Piece:
        """Replace an estimated count with the real one."""

        return replace(piece, tokens=self._count(piece.start, piece.end))

    def _window(self, span: _Piece) -> list[_Piece]:
        """Force-split a range with no structural cuts at the token limit.

        The bound gallops out from the limit (a token is at least one
        character for practical counters), then a binary search finds the
        longest prefix that fits, so each window costs about its own length.
        """

        content = self._content
        pieces: list[_Piece] = []
        position = span.start
        end = span.end
        while position < end:
            remaining = self._count(position, end) if end - position <= self._limit * 8 else None
            if remaining is not None and remaining <= self._limit:
                pieces.append(_Piece(position, end, span.scope, remaining))
                break
            low = position
            step = max(self._limit, 1)
            high = min(position + step, end)
            while high < end and self._count(position, high) <= self._limit:
                low = high
                step *= 2
                high = min(position + step, end)
            low = max(low, position + 1)
            while low < high:
                middle = (low + high + 1) // 2
                if self._count(position, middle) <= self._limit:
                    low = middle
                else:
                    high = middle - 1
            stop = low
            tokens = self._count(position, stop)
            if tokens > self._limit:
                raise ChunkValidationError(
                    f"{self._refiner.name} cannot fit a single character within "
                    f"maximum_tokens={self._limit}"
                )
            if (
                stop - 1 > position
                and content[stop - 1] == "\r"
                and content[stop : stop + 1] == "\n"
            ):
                stop -= 1
                tokens = self._count(position, stop)
            pieces.append(_Piece(position, stop, span.scope, tokens))
            position = stop
        return pieces


def _common_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    shared: list[str] = []
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        shared.append(a)
    return tuple(shared)
