"""One Markdown rendering for tables, shared by every source format.

HTML tables and Atlassian Document Format tables arrive as different trees but
carry the same thing: a grid with optional spans and an optional header row.
Rendering both through this module keeps one escaping and span policy, so a
table reads the same in chunk context whichever connector or parser produced
it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Spans and cell counts come from untrusted markup, and every span widens the
# whole grid, so a few bytes of `colspan` could otherwise allocate gigabytes.
MAX_TABLE_SPAN = 100
MAX_TABLE_CELLS = 50_000


@dataclass(frozen=True, slots=True)
class TableCell:
    """One already-rendered cell, with the spans it claims in the grid."""

    text: str = ""
    row_span: int = 1
    column_span: int = 1
    header: bool = False


def markdown_table(rows: Sequence[Sequence[TableCell]]) -> str:
    """Render rows of cells as a Markdown table, expanding row/column spans.

    A spanned cell keeps its text in the origin position and leaves the cells
    it covers empty, so every row still has the same column count. When the
    first row carries no header cell, positional `Column N` headers are
    generated: Markdown has no headerless table, and dropping the first data
    row into the header position would hide it.

    Spans are clamped to `MAX_TABLE_SPAN`, and a grid that would exceed
    `MAX_TABLE_CELLS` renders as plain rows of cell text instead: the words
    survive, the layout does not.
    """
    if not rows:
        return ""

    grid: list[list[str | None]] = []
    header_flags: list[bool] = []
    width = 1
    for row_index, row in enumerate(rows):
        _ensure_grid(grid, row_index + 1, 1)
        header_flags.append(any(cell.header for cell in row))
        column_index = 0
        for cell in row:
            while column_index < len(grid[row_index]) and grid[row_index][column_index] is not None:
                column_index += 1
            row_span = min(max(cell.row_span, 1), MAX_TABLE_SPAN)
            column_span = min(max(cell.column_span, 1), MAX_TABLE_SPAN)
            width = max(width, column_index + column_span)
            if max(len(grid), row_index + row_span) * width > MAX_TABLE_CELLS:
                return _plain_rows(rows)
            _ensure_grid(grid, row_index + row_span, column_index + column_span)
            origin = _with_span_labels(cell.text, row_span=row_span, column_span=column_span)
            for target_row in range(row_index, row_index + row_span):
                for target_column in range(column_index, column_index + column_span):
                    grid[target_row][target_column] = (
                        origin if target_row == row_index and target_column == column_index else ""
                    )
            column_index += column_span

    column_count = max(len(row) for row in grid)
    normalized = [
        [*(cell or "" for cell in row), *("" for _ in range(column_count - len(row)))]
        for row in grid
    ]
    if header_flags and header_flags[0]:
        header, data = normalized[0], normalized[1:]
    else:
        header = [f"Column {index + 1}" for index in range(column_count)]
        data = normalized
    return "\n".join(
        f"| {' | '.join(escape_table_cell(cell) for cell in row)} |"
        for row in (header, ["---"] * column_count, *data)
    )


def escape_table_cell(value: str) -> str:
    """Keep one cell's text inside its Markdown cell.

    A literal pipe would start a new column and a newline would end the row,
    so both are neutralized; angle brackets are escaped because a cell holding
    markup (a Jira comment quoting `<br>`, say) must not render as markup.
    """
    return value.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def positive_span(value: object) -> int:
    """Read a `rowspan`/`colspan` attribute, defaulting to one column or row."""
    try:
        return max(int(str(value or 1)), 1)
    except ValueError:
        return 1


def _plain_rows(rows: Sequence[Sequence[TableCell]]) -> str:
    """Render an oversized table as one line of cell text per row."""
    lines = (" ".join(cell.text for cell in row if cell.text) for row in rows)
    return "\n".join(line for line in lines if line)


def _with_span_labels(text: str, *, row_span: int, column_span: int) -> str:
    """Name a cell's spans in its text, which Markdown cannot express itself."""
    labels = [
        *([f"spans {row_span} rows"] if row_span > 1 else []),
        *([f"spans {column_span} columns"] if column_span > 1 else []),
    ]
    return f"{text} ({', '.join(labels)})" if labels else text


def _ensure_grid(grid: list[list[str | None]], rows: int, columns: int) -> None:
    """Grow the grid so `rows` x `columns` positions exist and are addressable."""
    while len(grid) < rows:
        grid.append([])
    for row in grid:
        row.extend(None for _ in range(columns - len(row)))
