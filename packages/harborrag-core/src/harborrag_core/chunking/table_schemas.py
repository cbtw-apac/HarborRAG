from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel


class TableProjectionType(StrEnum):
    ROUTE = "route"
    SCHEMA = "schema"
    ROWS = "rows"
    COLUMNS = "columns"
    MATRIX_ROW = "matrix_row"
    MATRIX_COLUMN = "matrix_column"
    TIME_WINDOW = "time_window"


class TableChunkLocator(StrictModel):
    """Locate an ordered row range within one deterministic table version."""

    table_id: str = Field(min_length=1)
    table_version_id: str = Field(min_length=1)
    row_start: int = Field(ge=0)
    row_end: int = Field(ge=0)
    column_count: int = Field(ge=1)
    key_column_indices: tuple[int, ...] = ()
    selected_column_indices: tuple[int, ...] = ()
    selected_columns: tuple[str, ...] = ()
    repeated_header_row_count: int = Field(default=0, ge=0)
    projection_type: TableProjectionType | None = None
    tab_path: tuple[str, ...] = ()
    fragment_index: int | None = Field(default=None, ge=0)
    fragment_count: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_table_range(self) -> TableChunkLocator:
        """Reject reversed rows and invalid or repeated key-column positions."""

        if self.row_end < self.row_start:
            raise ValueError("table row_end must not precede row_start")
        self._validate_indices(self.key_column_indices, "table key_column_indices")
        self._validate_indices(self.selected_column_indices, "selected_column_indices")
        self._validate_names_and_paths()
        self._validate_fragments()
        return self

    def _validate_indices(self, indices: tuple[int, ...], label: str) -> None:
        if len(set(indices)) != len(indices):
            raise ValueError(f"{label} must not contain duplicates")
        if any(index < 0 or index >= self.column_count for index in indices):
            raise ValueError(f"{label} must be within column_count")

    def _validate_names_and_paths(self) -> None:
        if self.selected_columns and (
            len(self.selected_columns) != len(self.selected_column_indices)
        ):
            raise ValueError("selected_columns must match selected_column_indices")
        if any(not column.strip() for column in self.selected_columns):
            raise ValueError("selected_columns must be non-empty")
        if any(not part.strip() for part in self.tab_path):
            raise ValueError("tab_path values must be non-empty")

    def _validate_fragments(self) -> None:
        if (self.fragment_index is None) != (self.fragment_count is None):
            raise ValueError("table fragment_index and fragment_count must be provided together")
        if (
            self.fragment_index is not None
            and self.fragment_count is not None
            and self.fragment_index >= self.fragment_count
        ):
            raise ValueError("fragment_index must be below fragment_count")
