"""Template for structure-aware text refiners.

A strategy only answers one question -- where may this text be cut, and how
strong is each cut -- by returning :class:`Cut` values from ``_propose``. The
base turns those into splits the same way for every strategy:

1. split the text at its strongest cuts, recursing into any piece that is still
   over the limit with the next-strongest cuts (structure first);
2. merge neighbouring pieces that came from the same parent back together while
   they fit (so a split never leaves needlessly small fragments); a piece that
   only introduces what follows (a heading, a comment or decorator) joins the
   piece after it, never the one before;
3. force-split any piece that has no cuts left with a token-bounded window;
4. settle pieces still below 1/8 of the limit (a tail such as ``}`` left by a
   split definition, a lone statement, whitespace): join a neighbour when the
   pair fits, otherwise move the boundary to a cut inside the neighbour that is
   at least as strong as the one it replaces, so both sides are big enough;
5. recount every split with the injected counter, apply overlap, check that the
   returned splits tile the source, and map them to source spans.

Only pieces cut by the token window are marked ``forced_split`` (boundary kind
``FORCED``); structural splits carry the refiner's ``split_kind``.

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

# A piece below maximum_tokens // SMALL_PIECE_DIVISOR is settled with a neighbour.
SMALL_PIECE_DIVISOR = 8


@dataclass(frozen=True, slots=True)
class Cut:
    """A position where ``request.content`` may be split.

    ``offset`` is the index of the first character of the following piece.
    Lower ``priority`` is a stronger boundary. ``scope`` describes the piece that
    starts at this offset (code refiners record enclosing definitions here).
    ``leads`` marks that piece as one that only introduces what follows it (a
    comment or decorator), so it joins the next piece rather than the previous;
    ``after_lead`` says the same of the piece that *ends* here (needed when that
    piece is the first of its run and so has no cut of its own before it).
    """

    offset: int
    priority: int
    scope: tuple[str, ...] = ()
    leads: bool = False
    after_lead: bool = False


@dataclass(frozen=True, slots=True)
class _CutIndex:
    """Cuts sorted by offset, with their offsets for range lookups."""

    cuts: list[Cut]
    offsets: list[int] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "offsets", [cut.offset for cut in self.cuts])

    def inside(self, start: int, end: int) -> list[Cut]:
        return self.cuts[bisect_right(self.offsets, start) : bisect_left(self.offsets, end)]

    def at(self, offset: int) -> Cut | None:
        position = bisect_left(self.offsets, offset)
        if position < len(self.offsets) and self.offsets[position] == offset:
            return self.cuts[position]
        return None


@dataclass(frozen=True, slots=True)
class _Piece:
    start: int
    end: int
    scope: tuple[str, ...]
    tokens: int
    forced: bool = False  # cut by the token window, not at a structural boundary
    leads: bool = False  # only introduces what follows (see Cut.leads)


class BaseTextRefiner(ABC):
    """Implement core ``TextRefiner`` for strategies that propose cut positions."""

    name: ClassVar[str]
    version: ClassVar[str]
    # Boundary kind of a structural (not forced) split; None keeps the request's kind.
    split_kind: ClassVar[SplitBoundaryKind | None] = None

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
        pieces = packing.settle_small(packing.descend(_Piece(0, len(content), (), total)))
        ranges = self._fold_invisible(content, self._with_overlap(request, pieces, index.offsets))

        results: list[TextSplit] = []
        for start, end, piece in ranges:
            text = content[start:end]
            count = self._count(text)
            if count > request.maximum_tokens:
                raise ChunkValidationError(
                    f"{self.name} produced a split above maximum_tokens ({count})"
                )
            results.append(
                TextSplit(
                    content=text,
                    token_count=count,
                    source_span=source_span(request, start, end),
                    boundary_kind=SplitBoundaryKind.FORCED
                    if piece.forced
                    else self.split_kind or request.boundary_kind,
                    structural_path=request.structural_path,
                    prefix="\n".join(piece.scope) if piece.scope else None,
                    forced_split=piece.forced,
                )
            )
        if not results:
            raise ChunkValidationError(f"{self.name} returned no content")
        # Checked on what is returned, so no piece can be dropped unnoticed.
        validate_coverage([(start, end) for start, end, _ in ranges], len(content))
        return tuple(results)

    def _fold_invisible(
        self,
        content: str,
        ranges: list[tuple[int, int, _Piece]],
    ) -> list[tuple[int, int, _Piece]]:
        """Join ranges the counter sees as zero tokens (e.g. whitespace) to a neighbour.

        A split must count at least one token, so such a range cannot stand
        alone; dropping it would leave a gap in the source.
        """

        folded: list[tuple[int, int, _Piece]] = []
        pending: int | None = None
        for start, end, piece in ranges:
            if self._count(content[start:end]) < 1:
                if folded:
                    folded[-1] = (folded[-1][0], end, folded[-1][2])
                elif pending is None:
                    pending = start
                continue
            folded.append((pending if pending is not None else start, end, piece))
            pending = None
        return folded

    @abstractmethod
    def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
        """Return every position where the request's content may be cut."""

    def _count(self, text: str) -> int:
        return self._token_counter.count(text)

    def _leads(self, text: str) -> bool:
        """Report whether ``text`` only introduces what follows it (a bare heading).

        Such a piece is joined to the next piece instead of trailing the
        previous one.
        """

        return False

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
    ) -> list[tuple[int, int, _Piece]]:
        """Extend each split backwards over at most ``overlap_tokens`` of context.

        Overlap starts only at a proposed cut so it never begins mid-word, and is
        skipped whenever it would break the limit. Callers that must not repeat
        content (line-preserving roles) ask for zero overlap.
        """

        overlap = request.overlap_tokens
        if overlap < 1:
            return [(piece.start, piece.end, piece) for piece in pieces]
        content = request.content
        ranges: list[tuple[int, int, _Piece]] = []
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
            ranges.append((start, piece.end, piece))
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
        self._hidden: set[int] = set()  # cut offsets ignored while descending a joined range

    def _count(self, start: int, end: int) -> int:
        return self._refiner._count(self._content[start:end])

    def descend(self, span: _Piece) -> list[_Piece]:
        if span.tokens <= self._limit:
            return [span]

        inside = [
            cut
            for cut in self._index.inside(span.start, span.end)
            if cut.offset not in self._hidden
        ]
        if not inside:
            return self._window(span)

        strongest = min(cut.priority for cut in inside)
        level = [cut for cut in inside if cut.priority == strongest]
        bounds = [(span.start, span.scope, span.leads)] + [
            (cut.offset, cut.scope, cut.leads) for cut in level
        ]
        children: list[list[_Piece]] = []
        for position, (child_start, child_scope, leads) in enumerate(bounds):
            child_end = bounds[position + 1][0] if position + 1 < len(bounds) else span.end
            leads = leads or (position < len(level) and level[position].after_lead)
            tokens = self._count(child_start, child_end)
            child = _Piece(child_start, child_end, child_scope, tokens, leads=leads)
            children.append(self.descend(child))
        return self._merge_siblings(children)

    def _merge_siblings(self, children: list[list[_Piece]]) -> list[_Piece]:
        """Greedily rejoin whole siblings while they fit.

        A child that itself had to be split stays as its own run of pieces; the
        only merge into it is a small preceding sibling joining its first piece,
        which removes a cut without creating a new one. Its tail is never
        extended into the next sibling, so the end of one definition is not
        glued onto an unrelated one (a tail that ends up tiny is settled later,
        see ``settle_small``). A sibling that leads (a bare heading, a comment
        or decorator: ``Cut.leads`` or the refiner's ``_leads``) never joins the
        piece before it, only the one after.

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
        pending: _Piece | None = None  # a run of leading pieces waiting for what they introduce

        def append(unit: _Piece) -> None:
            nonlocal open_piece
            joined = self._join(open_piece, unit, exact=exact) if open_piece is not None else None
            if joined is None:
                if open_piece is not None:
                    merged.append(self._settle(open_piece))
                open_piece = unit
            else:
                open_piece = joined

        def introduce(unit: _Piece) -> _Piece:
            """Prefix the pending leading run to ``unit`` when both fit together."""
            nonlocal pending
            if pending is None:
                return unit
            joined = self._join(pending, unit, exact=exact)
            if joined is None:  # they cannot share a piece: the run stands on its own
                append(pending)
                pending = None
                return unit
            pending = None
            return joined

        for child in children:
            if len(child) == 1:
                piece = child[0]
                if piece.leads or self._refiner._leads(self._content[piece.start : piece.end]):
                    piece = piece if piece.leads else replace(piece, leads=True)
                    if pending is None:
                        pending = piece
                        continue
                    run = self._join(pending, piece, exact=exact)
                    if run is None:
                        append(pending)
                        run = piece
                    pending = run
                    continue
                append(introduce(piece))
                continue
            first, *rest = child
            if pending is not None and self._join(pending, first, exact=exact) is None:
                # Split the leading run and the child as one unit, so the comment or
                # decorator opens the child's first piece instead of being cut off.
                first, *rest = self._descend_together(pending, child)
                pending = None
            first = introduce(first)
            if open_piece is not None:
                joined = self._join(open_piece, first, exact=exact)
                if joined is None:
                    merged.append(self._settle(open_piece))
                else:
                    first = joined
                open_piece = None
            merged.extend((self._settle(first), *rest))
        if pending is not None:  # nothing follows the leading run: let it trail
            append(pending)
        if open_piece is not None:
            merged.append(self._settle(open_piece))
        return merged

    def _descend_together(self, run: _Piece, child: list[_Piece]) -> list[_Piece]:
        """Descend ``run`` + ``child`` as one range, ignoring the cuts that separate them."""

        start, end = run.start, child[-1].end
        hidden = {cut.offset for cut in self._index.inside(start, child[0].start + 1)}
        saved, self._hidden = self._hidden, self._hidden | hidden
        try:
            return self.descend(_Piece(start, end, run.scope, self._count(start, end)))
        finally:
            self._hidden = saved

    def _join(self, left: _Piece, right: _Piece, *, exact: bool) -> _Piece | None:
        tokens = self._count(left.start, right.end) if exact else left.tokens + right.tokens
        if tokens > self._limit:
            return None
        return _Piece(
            left.start,
            right.end,
            _common_prefix(left.scope, right.scope),
            tokens,
            forced=left.forced or right.forced,
            leads=left.leads and right.leads,
        )

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
                pieces.append(_Piece(position, end, span.scope, remaining, forced=bool(pieces)))
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
            pieces.append(_Piece(position, stop, span.scope, tokens, forced=True))
            position = stop
        return pieces

    def settle_small(self, pieces: list[_Piece]) -> list[_Piece]:
        """Join or rebalance pieces below ``limit // SMALL_PIECE_DIVISOR`` with a neighbour.

        For each small piece, in order: join the previous piece if the pair
        fits; else move the boundary back to a cut inside the previous piece;
        else join the next piece; else move the boundary forward into it. A
        moved boundary only lands on a cut at least as strong as the one it
        replaces, so a definition that fits is never cut to make room. A small
        piece that none of these help is kept.
        """

        minimum = max(1, self._limit // SMALL_PIECE_DIVISOR)
        settled = list(pieces)
        kept: set[int] = set()
        while len(settled) > 1:
            small = next(
                (
                    position
                    for position, piece in enumerate(settled)
                    if piece.tokens < minimum and piece.start not in kept
                ),
                None,
            )
            if small is None:
                break
            if not self._absorb(settled, small, minimum):
                kept.add(settled[small].start)
        return settled

    def _absorb(self, pieces: list[_Piece], small: int, minimum: int) -> bool:
        for low in (small - 1, small):  # the pair (previous, small), then (small, next)
            high = low + 1
            if low < 0 or high >= len(pieces):
                continue
            left, right = pieces[low], pieces[high]
            tokens = self._count(left.start, right.end)
            if tokens <= self._limit:
                # The scope names what encloses the piece's start, where a reader lands.
                pieces[low : high + 1] = [
                    _Piece(
                        left.start,
                        right.end,
                        left.scope,
                        tokens,
                        forced=left.forced or right.forced,
                    )
                ]
                return True
            pair = self._rebalance(
                left, right, inside=left if low < small else right, minimum=minimum
            )
            if pair is not None:
                pieces[low : high + 1] = pair
                return True
        return False

    def _rebalance(
        self, left: _Piece, right: _Piece, *, inside: _Piece, minimum: int
    ) -> list[_Piece] | None:
        """Re-cut ``left + right`` at a cut inside ``inside`` so both sides fit and are not small.

        The replaced boundary is the cut at ``right.start``; only cuts at least
        as strong qualify. The strongest wins, then the most even pair.
        """

        boundary = self._index.at(right.start)
        ceiling = boundary.priority if boundary is not None else None
        by_priority: dict[int, list[Cut]] = {}
        for cut in self._index.inside(inside.start, inside.end):
            if ceiling is None or cut.priority <= ceiling:
                by_priority.setdefault(cut.priority, []).append(cut)
        best: tuple[int, Cut, int, int] | None = None
        for priority in sorted(by_priority):  # strongest level with a valid cut wins
            for cut in by_priority[priority]:
                first = self._count(left.start, cut.offset)
                second = self._count(cut.offset, right.end)
                if first > self._limit or second > self._limit or min(first, second) < minimum:
                    continue
                if best is None or abs(first - second) < best[0]:
                    best = (abs(first - second), cut, first, second)
            if best is not None:
                break
        if best is None:
            return None
        _, cut, first, second = best
        return [
            _Piece(left.start, cut.offset, left.scope, first, forced=left.forced),
            _Piece(cut.offset, right.end, cut.scope, second, forced=right.forced),
        ]


def _common_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    shared: list[str] = []
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        shared.append(a)
    return tuple(shared)
