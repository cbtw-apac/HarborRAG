from __future__ import annotations

from io import BytesIO
from typing import Any

from harborrag_adapters.parsers.common.validation import ParseResourceBudget
from harborrag_adapters.parsers.errors import ParseError

# openpyxl reports these when a worksheet's <dimension> record is absent or wrong.
_SHEET_MAX_ROWS = 1_048_576
_SHEET_MAX_COLUMNS = 16_384


def _declares_whole_sheet(workbook: Any) -> bool:
    return any(
        int(sheet.max_row or 0) >= _SHEET_MAX_ROWS
        or int(sheet.max_column or 0) >= _SHEET_MAX_COLUMNS
        for sheet in workbook.worksheets
    )


def open_data_workbook(load_workbook: Any, source_bytes: bytes) -> Any:
    """Open a workbook for text extraction with a dimension worth trusting.

    A <dimension> spanning the whole sheet says nothing about the real shape, and
    tool-generated workbooks emit exactly that. It is not merely uninformative: the
    declared size is guarded as hostile, and read-only mode pads `iter_rows` out to the
    declaration as well, so such a file is rejected however it is handled. Measured on a
    live space -- one Confluence attachment declared 1048576 x 26, really held 790 x 26,
    and failed `parser_rejected_document` on every single ingestion.

    A non-streaming load computes the dimension from the cells that exist. It is only
    reached for that case, and the caller has already run `open_guarded_zip`, so
    decompression-bomb shapes are rejected before this point.
    """

    workbook = load_workbook(
        BytesIO(source_bytes), read_only=True, data_only=True, keep_links=False
    )
    if not _declares_whole_sheet(workbook):
        return workbook
    workbook.close()
    return load_workbook(BytesIO(source_bytes), read_only=False, data_only=True, keep_links=False)


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
