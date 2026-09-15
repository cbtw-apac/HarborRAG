"""Shape-aware extractive rendering of a canonical table for chunk content.

Chunking used to read a table's ``element.content``, which every producer had
already flattened to tab-separated text: the header row was indistinguishable
from data, empty cells collapsed into adjacent tabs, and a cell spanning N grid
slots was emitted N times. Everything needed to do better -- column names,
header rows, the logical grid, spans, captions -- was already on the
``TableArtifact`` and simply discarded.

Rendering therefore belongs here, at chunk time, reading the artifact: it is the
only point that also knows the document title and the element's resolved section
path. ``element.content`` deliberately stays tab-separated, because
``FlatTableArtifactBuilder`` parses it back to build artifacts for every
non-Confluence source.

The returned ``prefix_line_count`` is what lets ``TableRowSplitter`` repeat the
preamble and header on every fragment of an oversized table instead of leaving
later fragments with no column names at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from harborrag_core.domain import TableArtifact

# Beyond this width a pipe table's rows are too long to read, so each row is
# rendered as one labeled record instead. Both layouts keep exactly one line per
# source row, which is what makes line-based fragmentation safe.
_MAXIMUM_GRID_COLUMNS = 10
_EMPTY_CELL = "-"

# Preamble lines in display order, paired with the order they are given up in
# when the profile's token budget cannot hold all of them. What makes a chunk
# locatable survives longest; the incidental notes go first.
_DOCUMENT = "document"
_SECTION = "section"
_TAB = "tab"
_LABEL = "label"
_SIZE = "size"
_MERGED = "merged"
_PREAMBLE_DISPLAY_ORDER = (_DOCUMENT, _SECTION, _TAB, _LABEL, _SIZE, _MERGED)
_PREAMBLE_DROP_ORDER = (_MERGED, _SIZE, _TAB, _LABEL, _SECTION, _DOCUMENT)


@dataclass(frozen=True, slots=True)
class TableView:
    """One rendered table plus the line count that must repeat per fragment."""

    content: str
    prefix_line_count: int
    row_count: int

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("rendered table view must not be empty")
        if self.prefix_line_count < 0 or self.row_count < 0:
            raise ValueError("rendered table view counts must not be negative")


@dataclass(frozen=True, slots=True)
class TableRenderContext:
    """Everything outside the artifact that shapes how it renders."""

    document_title: str
    section_path: tuple[str, ...] = ()
    table_total: int | None = None
    maximum_tokens: int | None = None
    count_tokens: Callable[[str], int] | None = None


def render_table_view(artifact: TableArtifact, context: TableRenderContext) -> TableView:
    """Render one table as self-describing text keyed off its canonical grid.

    ``maximum_tokens`` bounds the repeated part. An oversized table is split
    row-wise later, and every fragment repeats the preamble and header, so a
    preamble that leaves no room for a single row would make the table
    unsplittable. Supplying the profile's hard limit trims it instead.
    """

    parts = _preamble_parts(
        artifact,
        document_title=context.document_title,
        section_path=context.section_path or artifact.section_path,
        table_total=context.table_total,
    )
    header_lines, body_lines = _layout(artifact)
    if context.maximum_tokens is not None and context.count_tokens is not None:
        parts = _fit_preamble(
            parts,
            header_lines=header_lines,
            body_lines=body_lines,
            maximum_tokens=context.maximum_tokens,
            count_tokens=context.count_tokens,
        )
    preamble = tuple(parts[key] for key in _PREAMBLE_DISPLAY_ORDER if key in parts)
    prefix_lines = (*preamble, "", *header_lines) if preamble else header_lines
    content = "\n".join((*prefix_lines, *body_lines))
    return TableView(
        content=content,
        prefix_line_count=len(prefix_lines),
        row_count=len(body_lines),
    )


def _fit_preamble(
    parts: dict[str, str],
    *,
    header_lines: tuple[str, ...],
    body_lines: tuple[str, ...],
    maximum_tokens: int,
    count_tokens: Callable[[str], int],
) -> dict[str, str]:
    """Drop the least essential preamble lines until one row can still fit."""

    widest_row = max((count_tokens(line) for line in body_lines), default=0)
    remaining = dict(parts)
    for key in _PREAMBLE_DROP_ORDER:
        preamble = tuple(remaining[name] for name in _PREAMBLE_DISPLAY_ORDER if name in remaining)
        prefix_lines = (*preamble, "", *header_lines) if preamble else header_lines
        if count_tokens("\n".join(prefix_lines)) + widest_row <= maximum_tokens:
            return remaining
        remaining.pop(key, None)
    return remaining


def _layout(artifact: TableArtifact) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Select the header and body lines for this table's shape."""

    rows = _data_row_indices(artifact)
    if _is_key_value(artifact, rows):
        return (), _key_value_lines(artifact, rows)
    columns = tuple(range(artifact.column_count))
    if artifact.column_count > _MAXIMUM_GRID_COLUMNS:
        return (), _record_lines(artifact, rows, columns)
    return _grid_header_lines(artifact, columns), _grid_body_lines(artifact, rows, columns)


def _preamble_parts(
    artifact: TableArtifact,
    *,
    document_title: str,
    section_path: tuple[str, ...],
    table_total: int | None,
) -> dict[str, str]:
    parts: dict[str, str] = {}
    if document_title.strip():
        parts[_DOCUMENT] = f"Document: {document_title.strip()}"
    if section_path:
        parts[_SECTION] = f"Section: {' > '.join(section_path)}"
    if artifact.tab_path:
        parts[_TAB] = f"Tab: {' > '.join(artifact.tab_path)}"
    label = f"Table {artifact.ordinal + 1}"
    if table_total is not None:
        label = f"{label} of {table_total}"
    if artifact.caption and artifact.caption.strip():
        label = f"{label}: {artifact.caption.strip()}"
    parts[_LABEL] = label
    parts[_SIZE] = (
        f"Size: {artifact.row_count} "
        f"{'row' if artifact.row_count == 1 else 'rows'} x {artifact.column_count} "
        f"{'column' if artifact.column_count == 1 else 'columns'}"
    )
    if _has_merged_cells(artifact):
        parts[_MERGED] = "Merged cells: each value is shown once, at its first position"
    return parts


def _grid_header_lines(
    artifact: TableArtifact,
    columns: tuple[int, ...],
) -> tuple[str, ...]:
    names = tuple(_column_label(artifact, index) for index in columns)
    return (
        "| " + " | ".join(names) + " |",
        "| " + " | ".join("---" for _ in names) + " |",
    )


def _grid_body_lines(
    artifact: TableArtifact,
    rows: tuple[int, ...],
    columns: tuple[int, ...],
) -> tuple[str, ...]:
    return tuple(
        "| " + " | ".join(_display_cell(artifact, row_index, column) for column in columns) + " |"
        for row_index in rows
    )


def _key_value_lines(artifact: TableArtifact, rows: tuple[int, ...]) -> tuple[str, ...]:
    lines: list[str] = []
    for row_index in rows:
        key = _cell_text(artifact, row_index, 0)
        value = _cell_text(artifact, row_index, 1)
        lines.append(f"{key}: {value}" if value else f"{key}:")
    return tuple(lines)


def _record_lines(
    artifact: TableArtifact,
    rows: tuple[int, ...],
    columns: tuple[int, ...],
) -> tuple[str, ...]:
    labels = tuple(_column_label(artifact, index) for index in columns)
    lines: list[str] = []
    for row_index in rows:
        fields = [
            f"{label}: {_cell_text(artifact, row_index, column)}"
            for label, column in zip(labels, columns, strict=True)
            if _cell_text(artifact, row_index, column)
        ]
        lines.append(
            f"[row {row_index + 1}] " + "; ".join(fields) if fields else f"[row {row_index + 1}]"
        )
    return tuple(lines)


def _is_key_value(artifact: TableArtifact, rows: tuple[int, ...]) -> bool:
    """Report whether this table reads as label/value pairs rather than a grid.

    Confluence page-metadata tables (Status, Version, Author) are two columns
    whose header row is blank, and a pipe table is the worst possible rendering
    of them.
    """

    if artifact.column_count != 2 or not rows:
        return False
    if any(
        not _is_placeholder_header(name, index) for index, name in enumerate(artifact.column_names)
    ):
        return False
    return all(_cell_text(artifact, row_index, 0).strip() for row_index in rows)


def _is_placeholder_header(name: str, index: int) -> bool:
    """Report whether a column name carries no information about the column.

    A blank Confluence header cell is normalized to ``Column N`` before it
    reaches chunking, so an emptiness test alone never fires.
    """

    stripped = name.strip()
    return not stripped or stripped == f"Column {index + 1}"


def _data_row_indices(artifact: TableArtifact) -> tuple[int, ...]:
    return tuple(
        index for index in range(artifact.row_count) if index not in artifact.header_row_indices
    )


def _has_merged_cells(artifact: TableArtifact) -> bool:
    return any(cell.row_span > 1 or cell.column_span > 1 for cell in artifact.cells)


def _column_label(artifact: TableArtifact, index: int) -> str:
    hierarchy = artifact.header_hierarchy[index]
    levels = tuple(part.strip() for part in hierarchy if part.strip())
    if levels:
        return _escape(" > ".join(levels))
    name = artifact.column_names[index].strip()
    return _escape(name) if name else f"Column {index + 1}"


def _display_cell(artifact: TableArtifact, row_index: int, column_index: int) -> str:
    text = _cell_text(artifact, row_index, column_index)
    return _escape(text) if text else _EMPTY_CELL


def _cell_text(artifact: TableArtifact, row_index: int, column_index: int) -> str:
    """Return one slot's text, emitting a spanning cell only at its origin."""

    slot = artifact.logical_grid[row_index][column_index]
    if slot is None or slot.inherited:
        return ""
    cell = artifact.source_cell(row_index, column_index)
    return " ".join(cell.text.split()) if cell is not None else ""


def _escape(value: str) -> str:
    return value.replace("|", "\\|")


__all__ = ["TableRenderContext", "TableView", "render_table_view"]
