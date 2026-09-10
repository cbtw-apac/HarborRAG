"""Shape-aware table rendering: the chunk body a reader actually gets.

Chunking used to hand the tab-separated element body straight through, so a
table chunk arrived with its header indistinguishable from data, empty cells
collapsed into adjacent tabs, and every spanning cell repeated once per grid
slot. These tests pin the rendering that replaced it.
"""

from __future__ import annotations

import pytest

from harborrag_core.chunking import content_fingerprint
from harborrag_core.domain import (
    TableArtifact,
    TableCell,
    TableCellType,
    TableGridSlot,
)
from harborrag_engine.ingestion.chunking.transforms.table_text import (
    TableRenderContext,
    render_table_view,
)

pytestmark = [pytest.mark.unit]


def _artifact(
    rows: list[list[str]],
    *,
    header_rows: tuple[int, ...] = (0,),
    column_names: tuple[str, ...] | None = None,
    spans: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> TableArtifact:
    """Build one artifact whose logical grid honours the requested spans.

    Caption and section path do not affect grid validity, so tests that need
    them layer them on with ``model_copy`` rather than widening this signature.
    """

    spans = spans or {}
    column_count = max(len(row) for row in rows)
    grid: list[list[TableGridSlot | None]] = [[None] * column_count for _ in range(len(rows))]
    cells: list[TableCell] = []
    for row_index, row in enumerate(rows):
        for column_index, text in enumerate(row):
            if grid[row_index][column_index] is not None:
                continue
            row_span, column_span = spans.get((row_index, column_index), (1, 1))
            cell_id = f"c{row_index}-{column_index}"
            cells.append(
                TableCell(
                    cell_id=cell_id,
                    row_index=row_index,
                    column_index=column_index,
                    row_span=row_span,
                    column_span=column_span,
                    text=text,
                    cell_type=(
                        TableCellType.HEADER
                        if row_index in header_rows
                        else (TableCellType.EMPTY if not text else TableCellType.TEXT)
                    ),
                    is_header=row_index in header_rows,
                )
            )
            for spanned_row in range(row_index, row_index + row_span):
                for spanned_column in range(column_index, column_index + column_span):
                    grid[spanned_row][spanned_column] = TableGridSlot(
                        cell_id=cell_id,
                        inherited=(spanned_row, spanned_column) != (row_index, column_index),
                    )
    names = column_names or tuple(
        rows[0][index] or f"Column {index + 1}" for index in range(column_count)
    )
    return TableArtifact(
        table_id="table:1",
        table_version_id="table-version:1",
        document_id="document-1",
        document_version_id="document-version:1",
        source_version="source-1",
        source_block_id="table-element-1",
        ordinal=0,
        row_count=len(rows),
        column_count=column_count,
        header_row_indices=header_rows,
        column_names=names,
        header_hierarchy=tuple((name,) if name else () for name in names),
        cells=tuple(cells),
        logical_grid=tuple(tuple(row) for row in grid),
        content_hash=content_fingerprint("table"),
    )


def _context(**overrides: object) -> TableRenderContext:
    values: dict[str, object] = {"document_title": "Service Registry"}
    values.update(overrides)
    return TableRenderContext(**values)  # type: ignore[arg-type]


def test_grid_table_renders_a_labelled_pipe_table_under_a_locating_preamble() -> None:
    artifact = _artifact([["Service", "Owner"], ["ingest", "platform"]]).model_copy(
        update={"caption": "Owners", "section_path": ("Operations", "Ownership")}
    )

    view = render_table_view(artifact, _context())

    assert view.content.split("\n") == [
        "Document: Service Registry",
        "Section: Operations > Ownership",
        "Table 1: Owners",
        "Size: 2 rows x 2 columns",
        "",
        "| Service | Owner |",
        "| --- | --- |",
        "| ingest | platform |",
    ]
    assert view.row_count == 1
    assert view.prefix_line_count == 7
    assert view.content.splitlines()[view.prefix_line_count] == "| ingest | platform |"


def test_empty_cells_keep_their_column_position() -> None:
    artifact = _artifact([["Service", "Owner", "SLA"], ["ingest", "", "99.9%"]])

    view = render_table_view(artifact, _context())

    assert "| ingest | - | 99.9% |" in view.content


def test_a_spanning_cell_is_rendered_once_at_its_origin() -> None:
    """A cell covering N slots used to be emitted N times, inflating the chunk."""

    artifact = _artifact(
        [["Resource", "Cost", "Note"], ["shared total", "", ""]],
        spans={(1, 0): (1, 3)},
    )

    view = render_table_view(artifact, _context())

    assert view.content.count("shared total") == 1
    assert "| shared total | - | - |" in view.content
    assert "Merged cells: each value is shown once, at its first position" in view.content


def test_two_column_table_with_no_real_header_renders_as_key_value_pairs() -> None:
    """Confluence page-metadata tables carry ``Column N`` placeholder headers."""

    artifact = _artifact(
        [["", ""], ["Status", "Draft"], ["Owner", "Platform team"]],
        column_names=("Column 1", "Column 2"),
    )

    view = render_table_view(artifact, _context())

    assert "Status: Draft" in view.content
    assert "Owner: Platform team" in view.content
    assert "|" not in view.content


def test_wide_table_renders_one_labelled_record_per_row() -> None:
    columns = [f"c{index}" for index in range(12)]
    artifact = _artifact([columns, [f"v{index}" for index in range(12)]])

    view = render_table_view(artifact, _context())

    assert view.row_count == 1
    body = view.content.splitlines()[view.prefix_line_count]
    assert body.startswith("[row 2] c0: v0; c1: v1;")
    assert "c11: v11" in body


def test_preamble_is_trimmed_so_a_row_still_fits_the_hard_limit() -> None:
    """A preamble that crowds out every row would make the table unsplittable."""

    artifact = _artifact([["Service", "Owner"], ["ingest", "platform"]]).model_copy(
        update={
            "caption": "A caption long enough to matter for the budget",
            "section_path": ("An outer section", "An inner section"),
        }
    )
    widest_row = len("| ingest | platform |")

    generous = render_table_view(artifact, _context(maximum_tokens=500, count_tokens=len))
    tight = render_table_view(artifact, _context(maximum_tokens=60, count_tokens=len))

    assert "Section: An outer section > An inner section" in generous.content
    assert len(tight.content) < len(generous.content)
    assert "| Service | Owner |" in tight.content
    assert len(tight.content) - widest_row <= 60


def test_rendering_is_pure_and_repeatable() -> None:
    artifact = _artifact([["Service", "Owner"], ["ingest", "platform"]])

    assert render_table_view(artifact, _context()) == render_table_view(artifact, _context())
