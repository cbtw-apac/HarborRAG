from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from harborrag_core.contracts.chunking import (
    SourceSpan,
    SplitBoundaryKind,
    TokenCounter,
)

from ..config import ChunkingProfile
from ..schemas import ChunkCandidate, ChunkUnit

_UNIT_SEPARATOR = "\n\n"


def merge_source_spans(units: tuple[ChunkUnit, ...]) -> SourceSpan:
    """Merge provenance without inventing cross-element offsets."""

    spans = [unit.source_span for unit in units]
    element_ids = tuple(
        dict.fromkeys(element_id for span in spans for element_id in span.element_ids)
    )
    one_element = len(element_ids) == 1

    offsets = [
        (span.start_offset, span.end_offset)
        for span in spans
        if span.start_offset is not None and span.end_offset is not None
    ]
    start_offset = min(value[0] for value in offsets) if one_element and offsets else None
    end_offset = max(value[1] for value in offsets) if one_element and offsets else None
    if one_element and len(offsets) != len(spans):
        start_offset = end_offset = None

    lines = [
        (span.start_line, span.end_line)
        for span in spans
        if span.start_line is not None and span.end_line is not None
    ]
    start_line = min(value[0] for value in lines) if lines else None
    end_line = max(value[1] for value in lines) if lines else None
    if len(lines) != len(spans):
        start_line = end_line = None

    pages = [
        (span.page_start, span.page_end)
        for span in spans
        if span.page_start is not None and span.page_end is not None
    ]
    page_start = min(value[0] for value in pages) if pages else None
    page_end = max(value[1] for value in pages) if pages else None
    if len(pages) != len(spans):
        page_start = page_end = None

    return SourceSpan(
        start_offset=start_offset,
        end_offset=end_offset,
        start_line=start_line,
        end_line=end_line,
        page_start=page_start,
        page_end=page_end,
        element_ids=element_ids,
    )


def merge_metadata(units: tuple[ChunkUnit, ...]) -> Mapping[str, Any]:
    """Preserve common metadata and per-source metadata when values differ."""

    if len(units) == 1:
        return dict(units[0].metadata)
    common_keys = set(units[0].metadata)
    for unit in units[1:]:
        common_keys.intersection_update(unit.metadata)
    common: dict[str, Any] = {}
    for key in sorted(common_keys):
        first = units[0].metadata[key]
        try:
            equal = all(unit.metadata[key] == first for unit in units[1:])
        except Exception:
            equal = False
        if equal:
            common[key] = first
    common["source_units"] = tuple(
        {
            "anchor": unit.anchor,
            "source_element_ids": unit.source_span.element_ids,
            "metadata": dict(unit.metadata),
        }
        for unit in units
    )
    return common


class TokenBudgetPacker:
    """Pack adjacent compatible units without crossing hard boundaries."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def pack(
        self,
        units: tuple[ChunkUnit, ...],
        profile: ChunkingProfile,
    ) -> tuple[ChunkCandidate, ...]:
        """Pack compatible units up to the profile's token targets."""

        chunks: list[ChunkCandidate] = []
        current: list[ChunkUnit] = []

        for unit in units:
            if unit.token_count > profile.maximum_tokens:
                raise ValueError("oversized unit reached token packer")
            if not current:
                current = [unit]
                continue

            candidate_units = (*current, unit)
            candidate_content = _UNIT_SEPARATOR.join(item.content for item in candidate_units)
            candidate_count = self._token_counter.count(candidate_content)
            current_count = self._token_counter.count(
                _UNIT_SEPARATOR.join(item.content for item in current)
            )
            compatible = self.compatible(current[-1], unit)
            can_pack = compatible and (
                candidate_count <= profile.target_tokens
                or (
                    current_count < profile.minimum_tokens
                    and candidate_count <= profile.soft_maximum_tokens
                )
            )
            if can_pack:
                current.append(unit)
            else:
                chunks.append(self.build(tuple(current)))
                current = [unit]

        if current:
            chunks.append(self.build(tuple(current)))
        return tuple(chunks)

    def build(self, units: tuple[ChunkUnit, ...]) -> ChunkCandidate:
        """Build one candidate and recompute its exact token count."""

        content = _UNIT_SEPARATOR.join(unit.content for unit in units)
        boundary_kind = (
            units[0].boundary_kind
            if len({unit.boundary_kind for unit in units}) == 1
            else SplitBoundaryKind.PARAGRAPH
        )
        anchors = tuple(dict.fromkeys(unit.anchor for unit in units))
        anchor = (
            anchors[0]
            if len(anchors) == 1
            else "packed:" + json.dumps(anchors, ensure_ascii=False, separators=(",", ":"))
        )
        return ChunkCandidate(
            anchor=anchor,
            content=content,
            token_count=self._token_counter.count(content),
            role=units[0].role,
            structural_path=units[-1].structural_path,
            source_span=merge_source_spans(units),
            units=units,
            boundary_kind=boundary_kind,
            metadata=merge_metadata(units),
            forced_split=any(unit.forced_split for unit in units),
        )

    @staticmethod
    def compatible(left: ChunkUnit, right: ChunkUnit) -> bool:
        """Return whether adjacent units may share one canonical chunk."""

        return (
            left.merge_group == right.merge_group
            and left.role == right.role
            and left.structural_path == right.structural_path
            and not left.hard_boundary_after
            and not right.hard_boundary_before
            and not left.forced_split
            and not right.forced_split
        )
