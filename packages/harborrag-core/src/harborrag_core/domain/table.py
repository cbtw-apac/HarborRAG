from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from functools import cached_property
from math import isfinite

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking.source_schemas import SourceLocator

TableScalar = str | int | float | bool | None


class TableCellType(StrEnum):
    HEADER = "header"
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    EMPTY = "empty"
    MIXED = "mixed"


class TableCell(StrictModel):
    """One source cell, retained once even when it spans multiple grid slots."""

    cell_id: str = Field(min_length=1)
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    text: str = ""
    value: TableScalar = None
    cell_type: TableCellType = TableCellType.TEXT
    is_header: bool = False
    source_locator: SourceLocator = Field(default_factory=SourceLocator)
    nested_table_ids: tuple[str, ...] = ()

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: TableScalar) -> TableScalar:
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("table cell numeric values must be finite")
        return value


class TableGridSlot(StrictModel):
    """A logical-grid reference to a source cell."""

    cell_id: str = Field(min_length=1)
    inherited: bool = False


class TableColumnUnit(StrictModel):
    column_index: int = Field(ge=0)
    header: str = Field(min_length=1)
    unit: str = Field(min_length=1)


class TableKeyColumnCandidate(StrictModel):
    column_index: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: tuple[str, ...] = ()


class ParentTableCellLocator(StrictModel):
    table_id: str = Field(min_length=1)
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)


class TableArtifact(StrictModel):
    """Canonical table topology and logical grid stored separately from blocks."""

    table_id: str = Field(min_length=1)
    table_version_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_block_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    caption: str | None = None
    section_path: tuple[str, ...] = ()
    tab_path: tuple[str, ...] = ()
    row_count: int = Field(ge=1)
    column_count: int = Field(ge=1)
    header_row_indices: tuple[int, ...] = ()
    header_column_indices: tuple[int, ...] = ()
    column_names: tuple[str, ...]
    header_hierarchy: tuple[tuple[str, ...], ...]
    cells: tuple[TableCell, ...]
    logical_grid: tuple[tuple[TableGridSlot | None, ...], ...]
    units: tuple[TableColumnUnit, ...] = ()
    key_column_candidates: tuple[TableKeyColumnCandidate, ...] = ()
    source_locator: SourceLocator = Field(default_factory=SourceLocator)
    parent_cell: ParentTableCellLocator | None = None
    content_hash: str = Field(min_length=1)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_table(self) -> TableArtifact:
        self._validate_grid_shape()
        self._validate_header_shape()
        self._validate_source_cells()
        return self

    def _validate_grid_shape(self) -> None:
        if len(self.logical_grid) != self.row_count:
            raise ValueError("logical_grid row count does not match row_count")
        if any(len(row) != self.column_count for row in self.logical_grid):
            raise ValueError("logical_grid rows must match column_count")
        if len(self.column_names) != self.column_count:
            raise ValueError("column_names must match column_count")
        if len(self.header_hierarchy) != self.column_count:
            raise ValueError("header_hierarchy must match column_count")

    def _validate_header_shape(self) -> None:
        if any(not 0 <= index < self.row_count for index in self.header_row_indices):
            raise ValueError("header row indices must be within row_count")
        if any(not 0 <= index < self.column_count for index in self.header_column_indices):
            raise ValueError("header column indices must be within column_count")

    def _validate_source_cells(self) -> None:
        cell_ids = {cell.cell_id for cell in self.cells}
        if len(cell_ids) != len(self.cells):
            raise ValueError("table cell IDs must be unique")
        for cell in self.cells:
            if (
                cell.row_index + cell.row_span > self.row_count
                or cell.column_index + cell.column_span > self.column_count
            ):
                raise ValueError("table cell span exceeds logical grid")
        referenced = {slot.cell_id for row in self.logical_grid for slot in row if slot is not None}
        if any(slot_id not in cell_ids for slot_id in referenced):
            raise ValueError("logical_grid references an unknown source cell")
        if cell_ids - referenced:
            raise ValueError("table cells must be referenced by the logical grid")

    @cached_property
    def _cells_by_id(self) -> Mapping[str, TableCell]:
        return {cell.cell_id: cell for cell in self.cells}

    def source_cell(self, row_index: int, column_index: int) -> TableCell | None:
        """Resolve one logical slot to its retained source cell."""

        slot = self.logical_grid[row_index][column_index]
        if slot is None:
            return None
        return self._cells_by_id[slot.cell_id]
