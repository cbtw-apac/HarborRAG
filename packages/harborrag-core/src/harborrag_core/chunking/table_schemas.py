from __future__ import annotations

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel


class TableChunkLocator(StrictModel):
    """Locate an ordered row range within one deterministic table version."""

    table_id: str = Field(min_length=1)
    table_version_id: str = Field(min_length=1)
    row_start: int = Field(ge=0)
    row_end: int = Field(ge=0)
    column_count: int = Field(ge=1)
    key_column_indices: tuple[int, ...] = ()
    repeated_header_row_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_table_range(self) -> TableChunkLocator:
        """Reject reversed rows and invalid or repeated key-column positions."""

        if self.row_end < self.row_start:
            raise ValueError("table row_end must not precede row_start")
        if len(set(self.key_column_indices)) != len(self.key_column_indices):
            raise ValueError("table key_column_indices must not contain duplicates")
        if any(index < 0 or index >= self.column_count for index in self.key_column_indices):
            raise ValueError("table key_column_indices must be within column_count")
        described_row_count = self.row_end - self.row_start + 1
        if self.repeated_header_row_count > described_row_count:
            raise ValueError("repeated_header_row_count must not exceed the table row range")
        return self
