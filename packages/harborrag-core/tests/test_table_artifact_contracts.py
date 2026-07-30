from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_core.chunking import SourceLocator
from harborrag_core.domain import TableArtifact, TableCell, TableCellType, TableGridSlot


def test_table_artifact_retains_source_topology_without_copying_merged_values():
    merged = TableCell(
        cell_id="cell:merged",
        row_index=0,
        column_index=0,
        row_span=2,
        text="Service",
        value="Service",
        cell_type=TableCellType.HEADER,
        is_header=True,
    )
    value = TableCell(
        cell_id="cell:value",
        row_index=1,
        column_index=1,
        text="2",
        value=2,
        cell_type=TableCellType.NUMBER,
    )
    artifact = TableArtifact(
        table_id="table:1",
        table_version_id="table-version:1",
        document_id="document:1",
        document_version_id="document-version:1",
        source_version="1",
        source_block_id="source-table",
        ordinal=0,
        row_count=2,
        column_count=2,
        header_row_indices=(0,),
        column_names=("Service", "CPU"),
        header_hierarchy=(("Service",), ("CPU",)),
        cells=(merged, value),
        logical_grid=(
            (
                TableGridSlot(cell_id="cell:merged"),
                None,
            ),
            (
                TableGridSlot(cell_id="cell:merged", inherited=True),
                TableGridSlot(cell_id="cell:value"),
            ),
        ),
        content_hash="sha256",
        source_locator=SourceLocator(source_element_ids=("source-table",)),
    )

    assert len(artifact.cells) == 2
    assert artifact.logical_grid[1][0].inherited is True
    assert artifact.source_cell(1, 0) is merged


def _minimal_artifact_values() -> dict[str, object]:
    cell = TableCell(
        cell_id="cell",
        row_index=0,
        column_index=0,
        text="value",
        value="value",
    )
    return {
        "table_id": "table:1",
        "table_version_id": "table-version:1",
        "document_id": "document:1",
        "document_version_id": "document-version:1",
        "source_version": "1",
        "source_block_id": "source-table",
        "ordinal": 0,
        "row_count": 1,
        "column_count": 1,
        "column_names": ("Column",),
        "header_hierarchy": (("Column",),),
        "cells": (cell,),
        "logical_grid": ((TableGridSlot(cell_id="cell"),),),
        "content_hash": "sha256",
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"row_count": 2}, "row count"),
        ({"column_count": 2}, "column_count"),
        ({"column_names": ()}, "column_names"),
        ({"header_hierarchy": ()}, "header_hierarchy"),
        ({"header_row_indices": (1,)}, "header row"),
        ({"header_column_indices": (1,)}, "header column"),
    ],
)
def test_table_artifact_shape_invariants(change, message):
    values = _minimal_artifact_values()
    values.update(change)

    with pytest.raises(ValidationError, match=message):
        TableArtifact(**values)


def test_table_artifact_rejects_duplicate_cells_and_out_of_bounds_spans():
    values = _minimal_artifact_values()
    cell = values["cells"][0]  # type: ignore[index]
    values["cells"] = (cell, cell)
    with pytest.raises(ValidationError, match="unique"):
        TableArtifact(**values)

    values = _minimal_artifact_values()
    values["cells"] = (
        TableCell(
            cell_id="cell",
            row_index=0,
            column_index=0,
            row_span=2,
        ),
    )
    with pytest.raises(ValidationError, match="span"):
        TableArtifact(**values)

    with pytest.raises(ValidationError, match="finite"):
        TableCell(
            cell_id="cell",
            row_index=0,
            column_index=0,
            value=float("nan"),
        )


def test_empty_logical_grid_slot_resolves_to_no_source_cell():
    values = _minimal_artifact_values()
    values["cells"] = ()
    values["logical_grid"] = ((None,),)

    artifact = TableArtifact(**values)

    assert artifact.source_cell(0, 0) is None


def _spanning_cell_artifact_values() -> dict[str, object]:
    """One row: cell `a` spans columns 0-1, cell `c` sits alone at column 2.

    Column 3 is intentionally left `None` (no cell declared there) so slot
    mutations below can add an out-of-span reference without also removing
    a legitimate one.
    """

    spanning = TableCell(cell_id="a", row_index=0, column_index=0, row_span=1, column_span=2)
    solo = TableCell(cell_id="c", row_index=0, column_index=2)
    return {
        "table_id": "table:1",
        "table_version_id": "table-version:1",
        "document_id": "document:1",
        "document_version_id": "document-version:1",
        "source_version": "1",
        "source_block_id": "source-table",
        "ordinal": 0,
        "row_count": 1,
        "column_count": 4,
        "column_names": ("A", "A", "C", "D"),
        "header_hierarchy": (("A",), ("A",), ("C",), ("D",)),
        "cells": (spanning, solo),
        "logical_grid": (
            (
                TableGridSlot(cell_id="a"),
                TableGridSlot(cell_id="a", inherited=True),
                TableGridSlot(cell_id="c"),
                None,
            ),
        ),
        "content_hash": "sha256",
    }


def _mutate_row0(values: dict[str, object], replacements: dict[int, TableGridSlot | None]) -> None:
    row = list(values["logical_grid"][0])  # type: ignore[index]
    for column, slot in replacements.items():
        row[column] = slot
    values["logical_grid"] = (tuple(row),)


def test_table_artifact_rejects_a_slot_referencing_a_cell_outside_its_own_span():
    values = _spanning_cell_artifact_values()
    _mutate_row0(values, {3: TableGridSlot(cell_id="a")})

    with pytest.raises(ValidationError, match="outside every cell's declared span"):
        TableArtifact(**values)


def test_table_artifact_rejects_a_span_position_left_unfilled():
    values = _spanning_cell_artifact_values()
    _mutate_row0(values, {1: None})

    with pytest.raises(ValidationError, match="does not match its source cell span"):
        TableArtifact(**values)


def test_table_artifact_rejects_inherited_flag_mismatched_with_span_position():
    values = _spanning_cell_artifact_values()
    _mutate_row0(values, {1: TableGridSlot(cell_id="a", inherited=False)})

    with pytest.raises(ValidationError, match="inherited flag"):
        TableArtifact(**values)
