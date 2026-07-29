from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MatrixProjectionMode(StrEnum):
    ROWS = "rows"
    COLUMNS = "columns"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class TableClassificationThresholds:
    """Configurable shape thresholds with deterministic precedence."""

    small_maximum_rows: int = 50
    small_maximum_columns: int = 10
    small_maximum_tokens: int = 1500
    long_minimum_rows: int = 51
    wide_minimum_columns: int = 11
    large_minimum_rows: int = 5001
    large_minimum_cells: int = 100_001
    large_minimum_tokens: int = 100_001
    matrix_confidence: float = 0.75
    time_series_confidence: float = 0.70

    def __post_init__(self) -> None:
        counts = (
            self.small_maximum_rows,
            self.small_maximum_columns,
            self.small_maximum_tokens,
            self.long_minimum_rows,
            self.wide_minimum_columns,
            self.large_minimum_rows,
            self.large_minimum_cells,
            self.large_minimum_tokens,
        )
        if any(value < 1 for value in counts):
            raise ValueError("table classification thresholds must be positive")
        if self.long_minimum_rows <= self.small_maximum_rows:
            raise ValueError("long_minimum_rows must exceed small_maximum_rows")
        if self.wide_minimum_columns <= self.small_maximum_columns:
            raise ValueError("wide_minimum_columns must exceed small_maximum_columns")
        if not 0 <= self.matrix_confidence <= 1:
            raise ValueError("matrix_confidence must be between zero and one")
        if not 0 <= self.time_series_confidence <= 1:
            raise ValueError("time_series_confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class TableChunkingPolicy:
    """Table planning policy embedded in the Work 1 ChunkingPlan."""

    thresholds: TableClassificationThresholds = TableClassificationThresholds()
    target_rows_per_chunk: int = 20
    maximum_rows_per_chunk: int = 30
    target_tokens_per_chunk: int = 400
    boundary_row_overlap: int = 0
    maximum_columns_per_group: int = 10
    maximum_key_columns: int = 3
    key_column_confidence: float = 0.65
    maximum_evidence_chunks_per_table: int = 250
    maximum_row_groups_per_table: int = 250
    maximum_dense_table_tokens: int = 100_000
    route_preview_rows: int = 2
    large_table_evidence_enabled: bool = False
    matrix_projection_mode: MatrixProjectionMode = MatrixProjectionMode.BOTH
    explicit_key_columns: tuple[str | int, ...] = ()

    def __post_init__(self) -> None:
        positive = (
            self.target_rows_per_chunk,
            self.maximum_rows_per_chunk,
            self.target_tokens_per_chunk,
            self.maximum_columns_per_group,
            self.maximum_key_columns,
            self.maximum_evidence_chunks_per_table,
            self.maximum_row_groups_per_table,
            self.maximum_dense_table_tokens,
            self.route_preview_rows,
        )
        if any(value < 1 for value in positive):
            raise ValueError("table chunking limits must be positive")
        if self.target_rows_per_chunk > self.maximum_rows_per_chunk:
            raise ValueError("target_rows_per_chunk must not exceed its maximum")
        if self.boundary_row_overlap not in {0, 1}:
            raise ValueError("boundary_row_overlap must be zero or one")
        if not 0 <= self.key_column_confidence <= 1:
            raise ValueError("key_column_confidence must be between zero and one")
        if len(set(self.explicit_key_columns)) != len(self.explicit_key_columns):
            raise ValueError("explicit_key_columns must not contain duplicates")
