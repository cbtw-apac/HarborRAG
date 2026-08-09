from __future__ import annotations

from collections.abc import Callable

from harborrag_core.chunking import TableProjectionType
from harborrag_core.contracts import TokenCounter
from harborrag_core.domain import TableArtifact, TableCellType

from .grouping import (
    RowProjectionPlan,
    TableColumnGroupPlanner,
    TableRowGroupPlanner,
)
from .models import (
    PlannedTableChunk,
    TableChunkRole,
    TableClassification,
    TablePlan,
    TableShape,
)
from .policy import MatrixProjectionMode, TableChunkingPolicy
from .rendering import TableRenderer

type EvidenceStrategy = Callable[
    [TableArtifact, TableRenderer, TableClassification, TableChunkingPolicy],
    tuple[list[PlannedTableChunk], list[str]],
]


class TableChunkPlanner:
    """Resolve table shapes to bounded route, schema, and evidence plans."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._rows = TableRowGroupPlanner(token_counter)
        self._columns = TableColumnGroupPlanner()
        self._evidence_strategy: dict[TableShape, EvidenceStrategy] = {
            TableShape.SMALL: self._small_evidence,
            TableShape.LONG: self._long_evidence,
            TableShape.WIDE: self._wide_evidence,
            TableShape.LARGE: self._large_evidence,
            TableShape.MATRIX: self._matrix_evidence,
            TableShape.TIME_SERIES: self._time_series_evidence,
        }

    def plan(
        self,
        artifact: TableArtifact,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> TablePlan:
        renderer = TableRenderer(artifact)
        planned = self._routing_plans(artifact, classification)
        evidence, warnings = self._evidence_plans(
            artifact,
            renderer,
            classification,
            policy,
        )
        maximum = policy.maximum_evidence_chunks_per_table
        if len(evidence) > maximum:
            evidence = evidence[:maximum]
            warnings.append(
                f"table evidence chunk cap reached; retained first {maximum} planned chunks"
            )
        planned.extend(evidence)
        return TablePlan(
            classification=classification,
            chunks=tuple(planned),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _routing_plans(
        artifact: TableArtifact,
        classification: TableClassification,
    ) -> list[PlannedTableChunk]:
        all_columns = tuple(range(artifact.column_count))
        full_end = artifact.row_count - 1
        planned = [
            PlannedTableChunk(
                role=TableChunkRole.ROUTE,
                projection_type=TableProjectionType.ROUTE,
                row_start=0,
                row_end=full_end,
                selected_column_indices=all_columns,
            )
        ]
        if classification.shape in {
            TableShape.LARGE,
            TableShape.WIDE,
            TableShape.MATRIX,
            TableShape.TIME_SERIES,
        }:
            planned.append(
                PlannedTableChunk(
                    role=TableChunkRole.SCHEMA,
                    projection_type=TableProjectionType.SCHEMA,
                    row_start=0,
                    row_end=full_end,
                    selected_column_indices=all_columns,
                )
            )
        return planned

    def _evidence_plans(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> tuple[list[PlannedTableChunk], list[str]]:
        if not renderer.data_row_indices:
            return [], ["table contains no non-header rows for evidence chunking"]
        strategy = self._evidence_strategy[classification.shape]
        return strategy(artifact, renderer, classification, policy)

    def _small_evidence(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> tuple[list[PlannedTableChunk], list[str]]:
        del policy
        rows = renderer.data_row_indices
        projection = RowProjectionPlan(
            columns=tuple(range(artifact.column_count)),
            key_columns=classification.key_column_indices,
            projection_type=TableProjectionType.ROWS,
        )
        return [self._rows.exact(artifact, rows[0], rows[-1], projection)], []

    def _long_evidence(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> tuple[list[PlannedTableChunk], list[str]]:
        projection = RowProjectionPlan(
            columns=tuple(range(artifact.column_count)),
            key_columns=classification.key_column_indices,
            projection_type=TableProjectionType.ROWS,
        )
        return self._rows.plan(artifact, renderer, policy, projection), []

    def _wide_evidence(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> tuple[list[PlannedTableChunk], list[str]]:
        plans: list[PlannedTableChunk] = []
        column_groups = self._columns.plan(
            artifact.column_count,
            classification.key_column_indices,
            policy.maximum_columns_per_group,
        )
        for columns in column_groups:
            projection = RowProjectionPlan(
                columns=columns,
                key_columns=classification.key_column_indices,
                projection_type=TableProjectionType.COLUMNS,
            )
            plans.extend(self._rows.plan(artifact, renderer, policy, projection))
        return plans, []

    def _matrix_evidence(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> tuple[list[PlannedTableChunk], list[str]]:
        plans: list[PlannedTableChunk] = []
        if policy.matrix_projection_mode in {
            MatrixProjectionMode.ROWS,
            MatrixProjectionMode.BOTH,
        }:
            row_projection = RowProjectionPlan(
                columns=tuple(range(artifact.column_count)),
                key_columns=classification.key_column_indices,
                projection_type=TableProjectionType.MATRIX_ROW,
            )
            plans.extend(self._rows.plan(artifact, renderer, policy, row_projection))
        if policy.matrix_projection_mode in {
            MatrixProjectionMode.COLUMNS,
            MatrixProjectionMode.BOTH,
        }:
            plans.extend(
                self._matrix_column_plans(
                    artifact,
                    renderer,
                    classification,
                    policy,
                )
            )
        return plans, []

    def _matrix_column_plans(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> list[PlannedTableChunk]:
        plans: list[PlannedTableChunk] = []
        key_columns = classification.key_column_indices or (0,)
        for column in range(artifact.column_count):
            if column in key_columns:
                continue
            projection = RowProjectionPlan(
                columns=tuple(dict.fromkeys((*key_columns, column))),
                key_columns=key_columns,
                projection_type=TableProjectionType.MATRIX_COLUMN,
            )
            plans.extend(self._rows.plan(artifact, renderer, policy, projection))
        return plans

    def _time_series_evidence(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> tuple[list[PlannedTableChunk], list[str]]:
        time_column = (
            (classification.time_column_index,)
            if classification.time_column_index is not None
            else ()
        )
        key_columns = tuple(dict.fromkeys((*time_column, *classification.key_column_indices)))
        projection = RowProjectionPlan(
            columns=tuple(range(artifact.column_count)),
            key_columns=key_columns,
            projection_type=TableProjectionType.TIME_WINDOW,
        )
        return self._rows.plan(artifact, renderer, policy, projection), []

    def _large_evidence(
        self,
        artifact: TableArtifact,
        renderer: TableRenderer,
        classification: TableClassification,
        policy: TableChunkingPolicy,
    ) -> tuple[list[PlannedTableChunk], list[str]]:
        if not policy.large_table_evidence_enabled:
            return [], ["large table evidence disabled; route and schema chunks retained"]
        descriptive = self._descriptive_columns(artifact, renderer)
        if not descriptive:
            return [], ["large table has no descriptive columns suitable for dense evidence"]
        projection = RowProjectionPlan(
            columns=tuple(dict.fromkeys((*classification.key_column_indices, *descriptive))),
            key_columns=classification.key_column_indices,
            projection_type=TableProjectionType.ROWS,
        )
        return self._rows.plan(artifact, renderer, policy, projection), []

    @staticmethod
    def _descriptive_columns(
        artifact: TableArtifact,
        renderer: TableRenderer,
    ) -> tuple[int, ...]:
        columns: list[int] = []
        for column_index in range(artifact.column_count):
            cells = [renderer.cell(row, column_index) for row in renderer.data_row_indices[:100]]
            text_cells = [
                cell
                for cell in cells
                if cell is not None
                and cell.cell_type == TableCellType.TEXT
                and len(cell.text.strip()) >= 20
            ]
            if text_cells and len(text_cells) / max(len(cells), 1) >= 0.25:
                columns.append(column_index)
        return tuple(columns)
