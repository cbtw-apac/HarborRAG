from __future__ import annotations

from harborrag_core.domain import DocumentElement, TableArtifact


def build_table_evidence_elements(
    elements: tuple[DocumentElement, ...],
    tables: tuple[TableArtifact, ...],
) -> tuple[DocumentElement, ...]:
    """Replace table references with exact cells and append nested tables."""

    by_id = {table.table_id: table for table in tables}
    rendered_ids: set[str] = set()
    output: list[DocumentElement] = []
    for element in elements:
        table_id = element.metadata.get("table_id")
        table = by_id.get(str(table_id)) if table_id is not None else None
        if element.type != "table" or table is None:
            output.append(element)
            continue
        rendered_ids.add(table.table_id)
        output.append(
            DocumentElement(
                id=element.id,
                type="table",
                content=render_table(table),
                metadata=element.metadata,
            )
        )
    output.extend(
        _nested_table_element(table) for table in tables if table.table_id not in rendered_ids
    )
    return tuple(output)


def _nested_table_element(table: TableArtifact) -> DocumentElement:
    return DocumentElement(
        id=f"table-artifact:{table.table_id}",
        type="table",
        content=render_table(table),
        metadata={
            "table_id": table.table_id,
            "table_version_id": table.table_version_id,
            "section_path": table.section_path,
            "tab_path": table.tab_path,
            "source_block_id": table.source_block_id,
            "row_count": table.row_count,
            "column_count": table.column_count,
            "nested": table.parent_cell is not None,
        },
    )


def render_table(table: TableArtifact) -> str:
    cells = {cell.cell_id: cell for cell in table.cells}
    rendered = "\n".join(
        "\t".join(
            _flat_cell_text(cells[slot.cell_id].text) if slot is not None else "" for slot in row
        )
        for row in table.logical_grid
    )
    # Tables with no extractable cell text (e.g. image-only layout tables)
    # would otherwise render to pure whitespace, which the chunking
    # segmenter treats as empty content and silently drops — leaving the
    # canonical table artifact with no corresponding chunk and failing
    # projection verification. A non-empty placeholder keeps the table
    # represented by exactly one chunk, matching the canonical artifact.
    if rendered.strip():
        return rendered
    return _empty_table_placeholder(table)


def _empty_table_placeholder(table: TableArtifact) -> str:
    dimensions = (
        f"{table.row_count} row{'s' if table.row_count != 1 else ''} x "
        f"{table.column_count} column{'s' if table.column_count != 1 else ''}"
    )
    if table.caption:
        return f"Table: {table.caption} ({dimensions}, no extractable cell text)"
    return f"Table ({dimensions}, no extractable cell text)"


def _flat_cell_text(value: str) -> str:
    return " ".join(value.replace("\t", " ").split())
