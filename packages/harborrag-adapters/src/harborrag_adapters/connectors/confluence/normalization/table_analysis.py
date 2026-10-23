"""Table header, unit, cell type and key column inference.

Handles:
- Row and column header detection
- Header hierarchy and units from column names
- Typed cell values
- Scored key column candidates
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime

from harborrag_core.domain import (
    TableCell,
    TableCellType,
    TableColumnUnit,
    TableGridSlot,
    TableKeyColumnCandidate,
)

_UNIT_PATTERN = re.compile(r"(?:\(([^()]+)\)|\[([^\[\]]+)\])\s*$")
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_FLOAT_PATTERN = re.compile(r"^[+-]?(?:\d+\.\d*|\d*\.\d+)$")
_IDENTITY_HEADERS = frozenset(
    {"id", "key", "name", "service", "owner", "entity", "item", "resource"}
)


class TableArtifactAnalyzer:
    """Heuristics deriving headers, units, types and key candidates from a table topology."""

    @staticmethod
    def header_columns(
        cells: Sequence[TableCell],
        header_rows: tuple[int, ...],
    ) -> tuple[int, ...]:
        """Return the columns holding header cells outside the header rows."""

        return tuple(
            sorted(
                {
                    cell.column_index
                    for cell in cells
                    if cell.is_header and cell.row_index not in header_rows
                }
            )
        )

    @staticmethod
    def header_hierarchy(
        cells: Sequence[TableCell],
        grid: tuple[tuple[TableGridSlot | None, ...], ...],
        header_rows: tuple[int, ...],
        column_count: int,
    ) -> tuple[tuple[str, ...], ...]:
        """Return, per column, the header texts from top to bottom.

        A text equal to the one just above is skipped, so merged headers appear once.

        Args:
            cells: All table cells.
            grid: Logical grid of cell slots.
            header_rows: Indices of the header rows.
            column_count: Number of grid columns.

        Returns:
            One path per column; empty for columns without header rows.
        """

        by_id = {cell.cell_id: cell for cell in cells}
        paths: list[tuple[str, ...]] = []
        for column_index in range(column_count):
            values: list[str] = []
            for row_index in header_rows:
                slot = grid[row_index][column_index]
                text = by_id[slot.cell_id].text.strip() if slot is not None else ""
                if text and (not values or values[-1] != text):
                    values.append(text)
            paths.append(tuple(values))
        return tuple(paths)

    @staticmethod
    def units(column_names: tuple[str, ...]) -> tuple[TableColumnUnit, ...]:
        """Return the units of column names ending in ``(unit)`` or ``[unit]``."""

        units: list[TableColumnUnit] = []
        for index, name in enumerate(column_names):
            match = _UNIT_PATTERN.search(name)
            if match:
                units.append(
                    TableColumnUnit(
                        column_index=index,
                        header=name,
                        unit=next(value for value in match.groups() if value),
                    )
                )
        return tuple(units)

    @staticmethod
    def key_candidates(
        cells: Sequence[TableCell],
        grid: tuple[tuple[TableGridSlot | None, ...], ...],
        column_names: tuple[str, ...],
        header_rows: tuple[int, ...],
    ) -> tuple[TableKeyColumnCandidate, ...]:
        """Score columns as possible row identifiers and keep those scoring at least 0.5.

        The score combines a known identity header, value uniqueness, non-empty share and
        first-column position.

        Args:
            cells: All table cells.
            grid: Logical grid of cell slots.
            column_names: Resolved column names.
            header_rows: Indices of the header rows, excluded from scoring.

        Returns:
            Candidates sorted by descending confidence, then column index.
        """

        by_id = {cell.cell_id: cell for cell in cells}
        data_rows = [index for index in range(len(grid)) if index not in header_rows]
        candidates: list[TableKeyColumnCandidate] = []
        for column_index, header in enumerate(column_names):
            values = [
                by_id[slot.cell_id].text.strip()
                for row_index in data_rows
                if (slot := grid[row_index][column_index]) is not None
                and by_id[slot.cell_id].text.strip()
            ]
            non_null_ratio = len(values) / max(len(data_rows), 1)
            unique_ratio = len(set(values)) / max(len(values), 1)
            known_name = header.lower().strip() in _IDENTITY_HEADERS
            confidence = min(
                1.0,
                (0.4 if known_name else 0.0)
                + 0.35 * unique_ratio
                + 0.2 * non_null_ratio
                + (0.05 if column_index == 0 else 0.0),
            )
            if confidence >= 0.5:
                reasons = (
                    *(("known identity header",) if known_name else ()),
                    "high uniqueness" if unique_ratio >= 0.8 else "partial uniqueness",
                    "high non-null ratio" if non_null_ratio >= 0.8 else "partial coverage",
                )
                candidates.append(
                    TableKeyColumnCandidate(
                        column_index=column_index,
                        confidence=confidence,
                        reasons=reasons,
                    )
                )
        return tuple(sorted(candidates, key=lambda item: (-item.confidence, item.column_index)))

    @staticmethod
    def typed_value(
        text: str,
        is_header: bool,
    ) -> tuple[str | int | float | bool | None, TableCellType]:
        """Return a cell's typed value and type.

        Header cells keep their text. Dates and datetimes stay strings, and a datetime at
        midnight counts as a date.

        Args:
            text: Cell text.
            is_header: Whether the cell is a header cell.

        Returns:
            Value and type, trying empty, boolean, integer, float, ISO date, then text.
        """

        value = text.strip()
        if is_header:
            return value, TableCellType.HEADER
        if not value:
            return None, TableCellType.EMPTY
        if value.lower() in {"true", "false"}:
            return value.lower() == "true", TableCellType.BOOLEAN
        if _INTEGER_PATTERN.fullmatch(value):
            return int(value), TableCellType.NUMBER
        if _FLOAT_PATTERN.fullmatch(value):
            return float(value), TableCellType.NUMBER
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            cell_type = (
                TableCellType.DATE
                if parsed.time().isoformat() == "00:00:00"
                else TableCellType.DATETIME
            )
            return value, cell_type
        except ValueError:
            try:
                date.fromisoformat(value)
                return value, TableCellType.DATE
            except ValueError:
                return value, TableCellType.TEXT

    @staticmethod
    def topology_warnings(cells: Sequence[TableCell]) -> tuple[str, ...]:
        """Return a warning if any cell is merged across rows or columns."""

        if any(cell.row_span > 1 or cell.column_span > 1 for cell in cells):
            return ("merged cells retained through logical-grid inheritance",)
        return ()
