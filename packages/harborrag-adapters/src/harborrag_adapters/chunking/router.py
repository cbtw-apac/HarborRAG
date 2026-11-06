"""Pick the refiner that understands an oversized unit's structure."""

from __future__ import annotations

from harborrag_core.contracts.chunking import (
    SplitBoundaryKind,
    TextRefinementRequest,
    TextRefiner,
    TextSplit,
)

from .document_structure import looks_like_markdown

_CODE_KINDS = frozenset({SplitBoundaryKind.CODE_BLOCK, SplitBoundaryKind.CODE_SYMBOL})
_LINE_KINDS = frozenset(
    {
        SplitBoundaryKind.TABLE,
        SplitBoundaryKind.TABLE_ROW,
        SplitBoundaryKind.LINE,
        SplitBoundaryKind.JSON_PATH,
    }
)


class RoutingTextRefiner:
    """Implement core ``TextRefiner`` by dispatching on boundary kind and content.

    ======================================  ===========================
    request                                 refiner
    ======================================  ===========================
    code block / code symbol                code structure
    table, table row, line, JSON path       separators (lines)
    any other kind with explicit separators separators (caller wins)
    prose carrying Markdown structure       document structure
    other prose                             text structure
    ======================================  ===========================
    """

    def __init__(
        self,
        *,
        text: TextRefiner,
        document: TextRefiner,
        code: TextRefiner,
        separators: TextRefiner,
    ) -> None:
        self._text = text
        self._document = document
        self._code = code
        self._separators = separators

    def select(self, request: TextRefinementRequest) -> TextRefiner:
        """Return the refiner that will handle ``request``."""

        if request.boundary_kind in _CODE_KINDS:
            return self._code
        if request.boundary_kind in _LINE_KINDS or request.separators is not None:
            return self._separators
        if looks_like_markdown(request.content):
            return self._document
        return self._text

    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        return self.select(request).split(request)

    def fingerprint(self) -> str:
        """Identify every routed strategy for processing-identity hashes."""

        parts = (self._text, self._document, self._code, self._separators)
        return "routing:1(" + ",".join(_fingerprint(part) for part in parts) + ")"


def _fingerprint(refiner: TextRefiner) -> str:
    fingerprint = getattr(refiner, "fingerprint", None)
    if callable(fingerprint):
        return str(fingerprint())
    return type(refiner).__name__
