"""Source-span mapping and coverage checks shared by every text refiner."""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.contracts.chunking import SourceSpan, TextRefinementRequest
from harborrag_core.ingestion import ChunkValidationError


def source_span(request: TextRefinementRequest, start: int, end: int) -> SourceSpan:
    """Map a half-open ``[start, end)`` range of ``request.content`` to the source.

    Offsets are rebased on the request's own span; lines are inclusive, so the
    end line is the line of the last character in the range.
    """

    base = request.source_span
    base_offset = base.start_offset if base and base.start_offset is not None else 0

    start_line: int | None = None
    end_line: int | None = None
    if base and base.start_line is not None:
        start_line = base.start_line + request.content.count("\n", 0, start)
        end_line = base.start_line + request.content.count("\n", 0, max(end - 1, start))

    return SourceSpan(
        start_offset=base_offset + start,
        end_offset=base_offset + end,
        start_line=start_line,
        end_line=end_line,
        page_start=base.page_start if base else None,
        page_end=base.page_end if base else None,
        element_ids=base.element_ids if base else (),
    )


def validate_coverage(ranges: Sequence[tuple[int, int]], source_size: int) -> None:
    """Require ordered ranges that leave no part of the source uncovered.

    Ranges may overlap (refinement overlap), but never go backwards, skip
    content, or run past the source.
    """

    coverage_end = 0
    last_start = -1
    for start, end in ranges:
        if start < 0 or end <= start or end > source_size:
            raise ChunkValidationError(
                "text split contains invalid source offsets: "
                f"start={start}, end={end}, source_size={source_size}"
            )
        if start < last_start:
            raise ChunkValidationError(
                "text split source offsets are not ordered: "
                f"start={start} precedes previous start={last_start}"
            )
        if start > coverage_end:
            raise ChunkValidationError(
                f"text split lost source content between offset {coverage_end} and {start}"
            )
        last_start = start
        coverage_end = max(coverage_end, end)
    if coverage_end != source_size:
        raise ChunkValidationError(
            f"text split lost trailing source content: covered {coverage_end} of {source_size}"
        )
