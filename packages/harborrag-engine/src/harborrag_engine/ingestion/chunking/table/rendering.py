from __future__ import annotations

from collections.abc import Iterable

from harborrag_core.domain import TableArtifact, TableCell, TableCellType

from .models import PlannedTableChunk, TableChunkRole, TableClassification


class TableRenderer:
    """Create deterministic extractive table representations."""

    def __init__(self, artifact: TableArtifact) -> None:
        self.artifact = artifact
        self._cells = {cell.cell_id: cell for cell in artifact.cells}

    @property
    def data_row_indices(self) -> tuple[int, ...]:
        return tuple(
            index
            for index in range(self.artifact.row_count)
            if index not in self.artifact.header_row_indices
        )

    def cell(self, row_index: int, column_index: int) -> TableCell | None:
        slot = self.artifact.logical_grid[row_index][column_index]
        return self._cells.get(slot.cell_id) if slot is not None else None

    def cell_text(self, row_index: int, column_index: int) -> str:
        cell = self.cell(row_index, column_index)
        return cell.text if cell is not None else ""

    def render(
        self,
        classification: TableClassification,
        planned: PlannedTableChunk,
        preview_rows: int,
    ) -> str:
        if planned.role == TableChunkRole.ROUTE:
            return self._route(
                classification,
                planned.selected_column_indices,
                preview_rows,
            )
        if planned.role == TableChunkRole.SCHEMA:
            return self._schema(classification, planned.selected_column_indices)
        return self._evidence(
            range(planned.row_start, planned.row_end + 1),
            planned.selected_column_indices,
        )

    def evidence(
        self,
        row_start: int,
        row_end: int,
        columns: tuple[int, ...],
    ) -> str:
        """Render one faithful row-and-column view for token estimation."""

        return self._evidence(range(row_start, row_end + 1), columns)

    def search_text(
        self,
        rows: Iterable[int],
        columns: tuple[int, ...],
    ) -> str:
        exact = [self.artifact.column_names[column] for column in columns]
        for row_index in rows:
            exact.extend(self.cell_text(row_index, column) for column in columns)
        exact.extend(unit.unit for unit in self.artifact.units if unit.column_index in columns)
        return "\n".join(exact)

    def estimated_text(self) -> str:
        return self._evidence(
            range(self.artifact.row_count), tuple(range(self.artifact.column_count))
        )

    def column_types(self) -> tuple[str, ...]:
        values: list[str] = []
        data_rows = self.data_row_indices
        for column_index in range(self.artifact.column_count):
            types = {
                cell.cell_type.value
                for row_index in data_rows
                if (cell := self.cell(row_index, column_index)) is not None
                and cell.cell_type != TableCellType.EMPTY
            }
            values.append(next(iter(types)) if len(types) == 1 else ("mixed" if types else "empty"))
        return tuple(values)

    def time_range(self, column_index: int | None) -> tuple[str, str] | None:
        if column_index is None:
            return None
        values = [
            self.cell_text(row, column_index)
            for row in self.data_row_indices
            if self.cell_text(row, column_index)
        ]
        return (values[0], values[-1]) if values else None

    def _route(
        self,
        classification: TableClassification,
        columns: tuple[int, ...],
        preview_rows: int,
    ) -> str:
        lines = [
            f"Table shape: {classification.shape.value}",
            f"Rows: {self.artifact.row_count}",
            f"Columns: {self.artifact.column_count}",
            f"Headers: {' | '.join(self.artifact.column_names)}",
        ]
        if classification.key_column_indices:
            lines.append(
                "Key columns: "
                + " | ".join(
                    self.artifact.column_names[index] for index in classification.key_column_indices
                )
            )
        time_range = self.time_range(classification.time_column_index)
        if time_range:
            lines.append(f"Time range: {time_range[0]} to {time_range[1]}")
        preview = self.data_row_indices[:preview_rows]
        if preview:
            lines.append("Preview:")
            lines.append(self._evidence(preview, columns))
        return "\n".join(lines)

    def _schema(
        self,
        classification: TableClassification,
        columns: tuple[int, ...],
    ) -> str:
        types = self.column_types()
        units = {unit.column_index: unit.unit for unit in self.artifact.units}
        key_columns = set(classification.key_column_indices)
        lines = [
            f"Rows: {self.artifact.row_count}",
            f"Columns: {self.artifact.column_count}",
        ]
        for index in columns:
            details = [types[index]]
            if index in units:
                details.append(f"unit={units[index]}")
            if index in key_columns:
                details.append("key")
            hierarchy = " > ".join(self.artifact.header_hierarchy[index])
            if hierarchy:
                details.append(f"header={hierarchy}")
            lines.append(f"- {self.artifact.column_names[index]}: {', '.join(details)}")
        return "\n".join(lines)

    def _evidence(self, rows: Iterable[int], columns: tuple[int, ...]) -> str:
        lines = ["\t".join(self.artifact.column_names[index] for index in columns)]
        for row_index in rows:
            values = [
                self._render_cell(self.cell(row_index, column_index)) for column_index in columns
            ]
            lines.append("\t".join(values))
        return "\n".join(lines)

    @staticmethod
    def _render_cell(cell: TableCell | None) -> str:
        if cell is None:
            return ""
        value = (
            cell.text.replace("\t", " ").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        )
        if cell.nested_table_ids:
            references = ", ".join(cell.nested_table_ids)
            value = f"{value}\nNested table references: {references}".strip()
        return value
