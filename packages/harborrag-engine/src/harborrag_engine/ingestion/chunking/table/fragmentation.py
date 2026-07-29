from __future__ import annotations

from harborrag_core.contracts import TokenCounter

from .errors import TableChunkingError


class TokenBudgetFragmenter:
    """Split an oversized rendered view while repeating its evidence header."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def split(
        self,
        content: str,
        prefix: str,
        hard_maximum: int,
        *,
        repeat_header: bool,
    ) -> tuple[str, ...]:
        if self._token_counter.count(f"{prefix}\n\n{content}") <= hard_maximum:
            return (content,)
        header, separator, body = content.partition("\n")
        repeated = f"{header}\n" if repeat_header and separator else ""
        source = body if repeated else content
        fragments: list[str] = []
        cursor = 0
        while cursor < len(source):
            end = self._largest_fitting_end(
                source,
                cursor,
                prefix,
                repeated,
                hard_maximum,
            )
            if end <= cursor:
                raise TableChunkingError(
                    "table contextual prefix and repeated headers exhaust hard token limit"
                )
            fragments.append(f"{repeated}{source[cursor:end]}")
            cursor = end
        return tuple(fragment for fragment in fragments if fragment.strip())

    def _largest_fitting_end(
        self,
        source: str,
        start: int,
        prefix: str,
        repeated: str,
        hard_maximum: int,
    ) -> int:
        low = start + 1
        high = len(source)
        best = start
        while low <= high:
            midpoint = (low + high) // 2
            candidate = f"{prefix}\n\n{repeated}{source[start:midpoint]}"
            if self._token_counter.count(candidate) <= hard_maximum:
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best < len(source):
            boundary = source.rfind("\n", start + 1, best + 1)
            if boundary > start:
                return boundary + 1
        return best
