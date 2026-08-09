from __future__ import annotations

from typing import Any

from harborrag_adapters.parsers.common.validation import ParseResourceBudget
from harborrag_adapters.parsers.errors import ParseError


def guard_declared_table_size(
    *,
    rows: int,
    columns: int,
    budget: ParseResourceBudget,
) -> None:
    """Reject hostile worksheet dimensions before a library expands them."""

    if rows > budget.max_rows - budget.rows:
        raise ParseError(f"Table row count exceeds parser limit {budget.max_rows}")
    if rows * columns > budget.max_cells - budget.cells:
        raise ParseError(f"Table cell count exceeds parser limit {budget.max_cells}")


def openxml_cell_to_text(value: Any) -> str:
    """Convert openpyxl cell values into stable searchable text."""

    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def legacy_cell_to_text(cell: Any, datemode: int, xlrd: Any) -> str:
    """Convert xlrd cell values while preserving dates, booleans, and errors."""

    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return ""
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return str(xlrd.xldate_as_datetime(cell.value, datemode).isoformat())
        except (OverflowError, ValueError):
            return str(cell.value)
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        number = float(cell.value)
        return str(int(number)) if number.is_integer() else str(number)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return f"#ERROR:{cell.value}"
    return str(cell.value)


__all__ = ["guard_declared_table_size", "legacy_cell_to_text", "openxml_cell_to_text"]
