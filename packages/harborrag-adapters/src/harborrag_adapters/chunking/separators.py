"""Split at caller-chosen separators, strongest first, without any dependency.

Engine asks for line-preserving splits of tables, JSON and logs with explicit
separators such as ``("\\n", "")``. Each separator becomes a cut after every
occurrence, ranked by its position in the tuple; the trailing ``""`` fallback
is the base class's token window.
"""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.contracts.chunking import TextRefinementRequest

from ._base import BaseTextRefiner, Cut

LINE_SEPARATORS = ("\n", "")


class SeparatorRefiner(BaseTextRefiner):
    """Honour ``request.separators`` (default: line breaks) as ranked cuts."""

    name = "separators"
    version = "1"

    def _propose(self, request: TextRefinementRequest) -> Sequence[Cut]:
        content = request.content
        separators = request.separators or LINE_SEPARATORS
        cuts: list[Cut] = []
        for priority, separator in enumerate(separators):
            if not separator:
                continue
            position = content.find(separator)
            while position >= 0:
                cuts.append(Cut(position + len(separator), priority))
                position = content.find(separator, position + len(separator))
        return cuts
