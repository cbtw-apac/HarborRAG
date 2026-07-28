from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_core.contracts.chunking import SourceSpan


def integer_metadata(metadata: Mapping[str, Any], *keys: str) -> int | None:
    """Return the first integer metadata value, excluding booleans."""

    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def element_span(element_id: str, content: str, metadata: Mapping[str, Any]) -> SourceSpan:
    """Build the best source span available for a normalized element."""

    start_offset = integer_metadata(metadata, "start_offset")
    end_offset = integer_metadata(metadata, "end_offset")
    if start_offset is None or end_offset is None:
        start_offset, end_offset = 0, len(content)

    start_line = integer_metadata(metadata, "start_line", "line_start", "line")
    end_line = integer_metadata(metadata, "end_line", "line_end")
    if start_line is not None and end_line is None:
        end_line = start_line + content.count("\n")
    if end_line is not None and start_line is None:
        start_line = end_line - content.count("\n")

    page_start = integer_metadata(metadata, "page_start", "page")
    page_end = integer_metadata(metadata, "page_end")
    if page_start is not None and page_end is None:
        page_end = page_start
    if page_end is not None and page_start is None:
        page_start = page_end

    return SourceSpan(
        start_offset=start_offset,
        end_offset=end_offset,
        start_line=start_line,
        end_line=end_line,
        page_start=page_start,
        page_end=page_end,
        element_ids=(element_id,),
    )
