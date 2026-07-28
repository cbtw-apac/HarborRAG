from __future__ import annotations

from dataclasses import replace

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextRefiner,
    TokenCounter,
)

from ..config import ChunkingProfile
from ..schemas import ChunkUnit


class TableChunker:
    """Group complete rows and preserve headers as separate metadata."""

    def __init__(self, token_counter: TokenCounter, refiner: TextRefiner) -> None:
        self._token_counter = token_counter
        self._refiner = refiner

    def split(
        self,
        unit: ChunkUnit,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Split an oversized table into complete row groups."""

        if unit.token_count <= profile.maximum_tokens:
            return (unit,)

        rows = unit.content.splitlines(keepends=True)
        if not rows:
            return (unit,)
        header = rows[0].rstrip("\r\n")
        row_units = self._row_units(unit, rows, profile)
        groups = self._pack_rows(row_units, profile)
        results: list[ChunkUnit] = []
        for index, group in enumerate(groups):
            content = "".join(row.content for row in group)
            span = self._merge_same_element_spans(group)
            results.append(
                ChunkUnit(
                    anchor=unit.anchor,
                    content=content,
                    token_count=self._token_counter.count(content),
                    role="table",
                    structural_path=unit.structural_path,
                    source_span=span,
                    merge_group=f"{unit.merge_group}:part:{index}",
                    boundary_kind=SplitBoundaryKind.TABLE_ROW,
                    hard_boundary_before=True,
                    hard_boundary_after=True,
                    forced_split=True,
                    metadata={
                        **unit.metadata,
                        "table_header": (header if profile.repeat_table_headers else None),
                        "row_start": min(int(row.metadata["row_index"]) for row in group),
                        "row_end": max(int(row.metadata["row_index"]) for row in group),
                    },
                )
            )
        return tuple(results)

    def _row_units(
        self,
        unit: ChunkUnit,
        rows: list[str],
        profile: ChunkingProfile,
    ) -> list[ChunkUnit]:
        results: list[ChunkUnit] = []
        cursor = unit.source_span.start_offset or 0
        for row_index, row in enumerate(rows):
            count = self._token_counter.count(row)
            span = SourceSpan(
                start_offset=cursor,
                end_offset=cursor + len(row),
                start_line=(
                    unit.source_span.start_line + row_index
                    if unit.source_span.start_line is not None
                    else None
                ),
                end_line=(
                    unit.source_span.start_line + row_index
                    if unit.source_span.start_line is not None
                    else None
                ),
                page_start=unit.source_span.page_start,
                page_end=unit.source_span.page_end,
                element_ids=unit.source_span.element_ids,
            )
            cursor += len(row)
            if count <= profile.maximum_tokens:
                if count > 0 and row.strip():
                    results.append(
                        replace(
                            unit,
                            content=row,
                            token_count=count,
                            source_span=span,
                            boundary_kind=SplitBoundaryKind.TABLE_ROW,
                            metadata={**unit.metadata, "row_index": row_index},
                        )
                    )
                continue

            splits = self._refiner.split(
                TextRefinementRequest(
                    content=row,
                    maximum_tokens=profile.maximum_tokens,
                    overlap_tokens=0,
                    source_span=span,
                    boundary_kind=SplitBoundaryKind.TABLE_ROW,
                    structural_path=unit.structural_path,
                    preserve_whitespace=True,
                    separators=("\n", ""),
                )
            )
            for split in splits:
                results.append(
                    replace(
                        unit,
                        content=split.content,
                        token_count=split.token_count,
                        source_span=split.source_span or span,
                        boundary_kind=SplitBoundaryKind.TABLE_ROW,
                        forced_split=True,
                        metadata={**unit.metadata, "row_index": row_index},
                    )
                )
        return results

    def _pack_rows(
        self,
        rows: list[ChunkUnit],
        profile: ChunkingProfile,
    ) -> list[tuple[ChunkUnit, ...]]:
        groups: list[tuple[ChunkUnit, ...]] = []
        current: list[ChunkUnit] = []
        for row in rows:
            candidate = "".join(item.content for item in (*current, row))
            candidate_count = self._token_counter.count(candidate)
            if current and candidate_count > profile.target_tokens:
                groups.append(tuple(current))
                current = [row]
            else:
                current.append(row)
        if current:
            groups.append(tuple(current))

        if len(groups) > 1:
            last = groups[-1]
            last_text = "".join(row.content for row in last)
            if self._token_counter.count(last_text) < profile.minimum_tokens:
                merged = (*groups[-2], *last)
                merged_text = "".join(row.content for row in merged)
                if self._token_counter.count(merged_text) <= profile.maximum_tokens:
                    groups[-2:] = [merged]
        return groups

    @staticmethod
    def _merge_same_element_spans(group: tuple[ChunkUnit, ...]) -> SourceSpan:
        first = group[0].source_span
        last = group[-1].source_span
        return replace(
            first,
            end_offset=last.end_offset,
            end_line=last.end_line,
            page_end=last.page_end,
        )
