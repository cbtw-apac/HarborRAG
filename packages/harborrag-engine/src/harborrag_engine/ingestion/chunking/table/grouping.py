from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.chunking import TableProjectionType
from harborrag_core.contracts import TokenCounter
from harborrag_core.domain import TableArtifact

from .models import PlannedTableChunk, TableChunkRole
from .policy import TableChunkingPolicy
from .rendering import TableRenderer


@dataclass(frozen=True, slots=True)
class RowProjectionPlan:
    columns: tuple[int, ...]
    key_columns: tuple[int, ...]
    projection_type: TableProjectionType


class TableRowGroupPlanner:
    """Plan contiguous, token-aware table row groups."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def plan(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        policy: TableChunkingPolicy,
        projection: RowProjectionPlan,
    ) -> list[PlannedTableChunk]:
        groups: list[tuple[int, ...]] = []
        current: list[int] = []
        for row_index in renderer.data_row_indices:
            candidate = (*current, row_index)
            content = renderer.evidence(
                candidate[0],
                candidate[-1],
                projection.columns,
            )
            over_target = self._token_counter.count(content) > policy.target_tokens_per_chunk
            at_row_target = len(current) >= policy.target_rows_per_chunk
            at_hard_rows = len(current) >= policy.maximum_rows_per_chunk
            if current and (over_target or at_row_target or at_hard_rows):
                groups.append(tuple(current))
                current = [current[-1], row_index] if policy.boundary_row_overlap else [row_index]
            else:
                current.append(row_index)
        if current:
            groups.append(tuple(current))
        return [
            self.exact(artifact, group[0], group[-1], projection)
            for group in groups[: policy.maximum_row_groups_per_table]
        ]

    @staticmethod
    def exact(
        artifact: TableArtifact,
        row_start: int,
        row_end: int,
        projection: RowProjectionPlan,
    ) -> PlannedTableChunk:
        return PlannedTableChunk(
            role=TableChunkRole.EVIDENCE,
            projection_type=projection.projection_type,
            row_start=row_start,
            row_end=row_end,
            selected_column_indices=projection.columns,
            repeated_key_column_indices=tuple(
                index for index in projection.key_columns if index in projection.columns
            ),
            repeated_header_row_count=len(artifact.header_row_indices),
        )


class TableColumnGroupPlanner:
    """Plan bounded column groups while repeating selected key columns."""

    @staticmethod
    def plan(
        column_count: int,
        key_columns: tuple[int, ...],
        maximum_columns: int,
    ) -> tuple[tuple[int, ...], ...]:
        keys = tuple(dict.fromkeys(key_columns))
        non_keys = tuple(index for index in range(column_count) if index not in keys)
        capacity = max(maximum_columns - len(keys), 1)
        return tuple(
            tuple(dict.fromkeys((*keys, *non_keys[start : start + capacity])))
            for start in range(0, len(non_keys), capacity)
        ) or (keys,)
