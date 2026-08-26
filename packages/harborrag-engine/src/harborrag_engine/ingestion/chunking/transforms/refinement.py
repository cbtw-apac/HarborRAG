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
from ..errors import OversizedChunkError
from ..schemas import ChunkUnit


class TableRowSplitter:
    """Split an oversized inline table into bounded complete row groups."""

    def __init__(self, token_counter: TokenCounter, refiner: TextRefiner) -> None:
        self._token_counter = token_counter
        self._refiner = refiner

    def split(
        self,
        unit: ChunkUnit,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Split one oversized table while retaining its header and locators."""

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
                if not split.content.strip():
                    continue
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


class OversizedUnitRefiner:
    """Enforce the hard token limit without crossing a source boundary."""

    _LINE_PRESERVING_ROLES = frozenset({"code", "json", "log", "table"})

    def __init__(self, refiner: TextRefiner, table_splitter: TableRowSplitter) -> None:
        self._refiner = refiner
        self._table_splitter = table_splitter

    def refine(
        self,
        units: tuple[ChunkUnit, ...],
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Refine every oversized unit while preserving source order."""

        results: list[ChunkUnit] = []
        for unit in units:
            results.extend(self.refine_one(unit, profile))
        return tuple(results)

    def refine_one(
        self,
        unit: ChunkUnit,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Refine one unit until every result satisfies the hard limit."""

        if unit.token_count <= profile.maximum_tokens:
            return (unit,)
        if unit.role == "table":
            splits = self._table_splitter.split(unit, profile)
            self._validate(splits, profile)
            return splits

        line_preserving = unit.role in self._LINE_PRESERVING_ROLES
        text_splits = self._refiner.split(
            TextRefinementRequest(
                content=unit.content,
                maximum_tokens=profile.maximum_tokens,
                overlap_tokens=0 if line_preserving else profile.overlap_tokens,
                source_span=unit.source_span,
                boundary_kind=unit.boundary_kind,
                structural_path=unit.structural_path,
                preserve_whitespace=line_preserving,
                separators=("\n", "") if line_preserving else None,
            )
        )
        retained = tuple(split for split in text_splits if split.content.strip())
        refined = tuple(
            replace(
                unit,
                content=split.content,
                token_count=split.token_count,
                source_span=split.source_span or unit.source_span,
                merge_group=f"{unit.merge_group}:part:{index}",
                boundary_kind=split.boundary_kind,
                hard_boundary_before=True,
                hard_boundary_after=True,
                forced_split=True,
                metadata={**unit.metadata, "local_part_index": index},
            )
            for index, split in enumerate(retained)
        )
        self._validate(refined, profile)
        return refined

    @staticmethod
    def _validate(
        units: tuple[ChunkUnit, ...],
        profile: ChunkingProfile,
    ) -> None:
        if not units:
            raise OversizedChunkError("oversized refiner returned no units")
        if any(unit.token_count > profile.maximum_tokens for unit in units):
            raise OversizedChunkError("refiner returned a unit above maximum_tokens")
