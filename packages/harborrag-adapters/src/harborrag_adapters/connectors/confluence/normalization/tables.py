"""Table node conversion into canonical ``TableArtifact`` objects.

Handles:
- Logical grid placement with row and column spans
- Nested tables as artifacts linked to their parent cell
- Header, unit and key column analysis through ``TableArtifactAnalyzer``
"""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.chunking import (
    CanonicalIdentityBuilder,
    SourceLocator,
    canonical_identity_payload,
    content_fingerprint,
    encoded_identifier,
)
from harborrag_core.domain import (
    ParentTableCellLocator,
    TableArtifact,
    TableCell,
    TableGridSlot,
)

from .errors import TableExtractionError
from .nodes import ConfluenceNode
from .table_analysis import TableArtifactAnalyzer


class TableArtifactBuilder:
    """Builder of a canonical table and its nested table artifacts.

    Shares the page's identity builder so ids stay consistent across tables and sections.
    """

    def __init__(
        self,
        *,
        document_id: str,
        document_version_id: str,
        source_version: str,
        source_url: str,
        identity: CanonicalIdentityBuilder | None = None,
    ) -> None:
        """Store the page identity values.

        Args:
            document_id: Document id of the page.
            document_version_id: Document version id of the page.
            source_version: Source version of the page.
            source_url: Canonical URL of the page.
            identity: Identity builder for ids; a new one if omitted.
        """

        self._document_id = document_id
        self._document_version_id = document_version_id
        self._source_version = source_version
        self._source_url = source_url
        self._identity = identity or CanonicalIdentityBuilder()

    def build(
        self,
        node: ConfluenceNode,
        *,
        ordinal: int,
        section_path: tuple[str, ...],
        tab_path: tuple[str, ...],
        parent_cell: ParentTableCellLocator | None = None,
    ) -> tuple[TableArtifact, ...]:
        """Build a table artifact and the artifacts of its nested tables.

        Args:
            node: Table node.
            ordinal: Position of the table among its siblings.
            section_path: Enclosing section path.
            tab_path: Enclosing tab path.
            parent_cell: Cell holding this table, for a nested table.

        Returns:
            The table artifact first, followed by nested artifacts.

        Raises:
            TableExtractionError: If the table has no rows or cells, or cell spans overlap.
        """

        rows = self._table_rows(node)
        if not rows:
            raise TableExtractionError(f"Confluence table {node.source_id!r} has no source rows")
        location: dict[str, object] = {
            "source_block_id": node.source_id,
            "ordinal": ordinal,
            "tab_path": tab_path,
        }
        if parent_cell is not None:
            location["parent_cell"] = parent_cell.model_dump()
        table_id = self._identity.table_id(
            document_id=self._document_id,
            section_path=section_path,
            stable_table_location=location,
        )
        cells, grid, nested = self._build_cells(
            rows,
            table_id=table_id,
            section_path=section_path,
            tab_path=tab_path,
        )
        if not cells:
            raise TableExtractionError(f"Confluence table {node.source_id!r} has no source cells")
        row_count = len(grid)
        column_count = max(len(row) for row in grid)
        normalized_grid = tuple(
            (*row, *(None for _ in range(column_count - len(row)))) for row in grid
        )
        header_rows = self._leading_header_rows(rows)
        header_columns = TableArtifactAnalyzer.header_columns(cells, header_rows)
        header_hierarchy = TableArtifactAnalyzer.header_hierarchy(
            cells,
            normalized_grid,
            header_rows,
            column_count,
        )
        column_names = tuple(
            " / ".join(path) if path else f"Column {index + 1}"
            for index, path in enumerate(header_hierarchy)
        )
        units = TableArtifactAnalyzer.units(column_names)
        candidates = TableArtifactAnalyzer.key_candidates(
            cells,
            normalized_grid,
            column_names,
            header_rows,
        )
        topology = {
            "cells": [
                {
                    "row": cell.row_index,
                    "column": cell.column_index,
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                    "text": cell.text,
                    "value": cell.value,
                    "header": cell.is_header,
                    "nested_tables": cell.nested_table_ids,
                }
                for cell in cells
            ],
            "row_count": row_count,
            "column_count": column_count,
        }
        content_hash = content_fingerprint(canonical_identity_payload(topology))
        artifact = TableArtifact(
            table_id=table_id,
            table_version_id=self._identity.table_version_id(
                table_id=table_id,
                source_version=self._source_version,
                content_hash=content_hash,
            ),
            document_id=self._document_id,
            document_version_id=self._document_version_id,
            source_version=self._source_version,
            source_block_id=node.source_id,
            ordinal=ordinal,
            caption=self._caption(node),
            section_path=section_path,
            tab_path=tab_path,
            row_count=row_count,
            column_count=column_count,
            header_row_indices=header_rows,
            header_column_indices=header_columns,
            column_names=column_names,
            header_hierarchy=header_hierarchy,
            cells=tuple(cells),
            logical_grid=normalized_grid,
            units=units,
            key_column_candidates=candidates,
            source_locator=SourceLocator(
                uri=self._source_url,
                source_element_ids=(node.source_id,),
            ),
            parent_cell=parent_cell,
            content_hash=content_hash,
            warnings=TableArtifactAnalyzer.topology_warnings(cells),
        )
        return (artifact, *nested)

    def _build_cells(
        self,
        rows: Sequence[ConfluenceNode],
        *,
        table_id: str,
        section_path: tuple[str, ...],
        tab_path: tuple[str, ...],
    ) -> tuple[list[TableCell], list[list[TableGridSlot | None]], list[TableArtifact]]:
        """Place every cell on the logical grid and build the cells and nested tables.

        Each cell starts at the first free column of its row. Covered slots point to the
        cell and only the anchor slot is not inherited. Raises ``TableExtractionError`` if
        spans overlap.
        """

        cells: list[TableCell] = []
        grid: list[list[TableGridSlot | None]] = []
        nested: list[TableArtifact] = []
        for row_index, row in enumerate(rows):
            self._ensure_grid(grid, row_index + 1, 1)
            for cell_node in self._row_cells(row):
                column_index = self._next_open_column(grid[row_index])
                # TODO: cap rowspan and colspan; huge values allocate an oversized grid.
                row_span = self._span(cell_node, "rowspan", "rowSpan")
                column_span = self._span(cell_node, "colspan", "colSpan")
                self._ensure_grid(
                    grid,
                    row_index + row_span,
                    column_index + column_span,
                )
                cell_id = encoded_identifier(
                    "table-cell",
                    {
                        "table_id": table_id,
                        "row": row_index,
                        "column": column_index,
                        "source_id": cell_node.source_id,
                    },
                )
                nested_nodes = self._nested_tables(cell_node)
                nested_ids: list[str] = []
                # TODO: bound table nesting depth; deep nesting raises RecursionError.
                for nested_ordinal, nested_node in enumerate(nested_nodes):
                    artifacts = self.build(
                        nested_node,
                        ordinal=nested_ordinal,
                        section_path=section_path,
                        tab_path=tab_path,
                        parent_cell=ParentTableCellLocator(
                            table_id=table_id,
                            row_index=row_index,
                            column_index=column_index,
                        ),
                    )
                    nested.extend(artifacts)
                    nested_ids.append(artifacts[0].table_id)
                text = cell_node.visible_text(exclude_kinds=frozenset({"table"}))
                value, cell_type = TableArtifactAnalyzer.typed_value(
                    text,
                    cell_node.kind == "table_header",
                )
                cell = TableCell(
                    cell_id=cell_id,
                    row_index=row_index,
                    column_index=column_index,
                    row_span=row_span,
                    column_span=column_span,
                    text=text,
                    value=value,
                    cell_type=cell_type,
                    is_header=cell_node.kind == "table_header",
                    source_locator=SourceLocator(
                        uri=self._source_url,
                        source_element_ids=(cell_node.source_id,),
                    ),
                    nested_table_ids=tuple(nested_ids),
                )
                cells.append(cell)
                for target_row in range(row_index, row_index + row_span):
                    for target_column in range(
                        column_index,
                        column_index + column_span,
                    ):
                        if grid[target_row][target_column] is not None:
                            raise TableExtractionError(
                                f"Confluence table {table_id!r} contains overlapping cell spans"
                            )
                        grid[target_row][target_column] = TableGridSlot(
                            cell_id=cell_id,
                            inherited=(target_row != row_index or target_column != column_index),
                        )
        return cells, grid, nested

    @staticmethod
    def _ensure_grid(
        grid: list[list[TableGridSlot | None]],
        rows: int,
        columns: int,
    ) -> None:
        """Grow the grid to ``rows`` rows and pad every row with None to ``columns`` slots."""

        while len(grid) < rows:
            grid.append([])
        for row in grid:
            row.extend(None for _ in range(columns - len(row)))

    @staticmethod
    def _next_open_column(row: list[TableGridSlot | None]) -> int:
        """Return the first unfilled column of a row, appending one if the row is full."""

        try:
            return row.index(None)
        except ValueError:
            row.append(None)
            return len(row) - 1

    @classmethod
    def _table_rows(cls, table: ConfluenceNode) -> tuple[ConfluenceNode, ...]:
        """Return the table's rows, excluding rows of nested tables."""

        return tuple(cls._descendants(table, {"table_row"}, stop={"table"}))

    @classmethod
    def _row_cells(cls, row: ConfluenceNode) -> tuple[ConfluenceNode, ...]:
        """Return a row's header and data cells, excluding cells of nested tables."""

        return tuple(
            cls._descendants(
                row,
                {"table_cell", "table_header"},
                stop={"table", "table_row"},
            )
        )

    @classmethod
    def _nested_tables(cls, cell: ConfluenceNode) -> tuple[ConfluenceNode, ...]:
        """Return the tables directly nested in a cell."""

        return tuple(cls._descendants(cell, {"table"}, stop={"table"}))

    @classmethod
    def _descendants(
        cls,
        node: ConfluenceNode,
        kinds: set[str],
        *,
        stop: set[str],
    ) -> list[ConfluenceNode]:
        """Return descendants of ``kinds`` in document order, not entering ``stop`` kinds."""

        found: list[ConfluenceNode] = []
        for child in node.children:
            if child.kind in kinds:
                found.append(child)
                continue
            if child.kind not in stop:
                found.extend(cls._descendants(child, kinds, stop=stop))
        return found

    @staticmethod
    def _span(node: ConfluenceNode, *keys: str) -> int:
        """Return the first present span attribute as an integer of at least 1, else 1."""

        value = next((node.attributes[key] for key in keys if key in node.attributes), 1)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return 1
        try:
            return max(int(value), 1)
        except ValueError:
            return 1

    @staticmethod
    def _caption(node: ConfluenceNode) -> str | None:
        """Return the ``caption`` attribute, else the text of a caption child, else None."""

        value = node.attributes.get("caption")
        if value and str(value).strip():
            return str(value).strip()
        caption = next((child for child in node.children if child.kind == "caption"), None)
        return caption.visible_text() if caption is not None else None

    @classmethod
    def _leading_header_rows(cls, rows: Sequence[ConfluenceNode]) -> tuple[int, ...]:
        """Return the indices of the leading rows made only of header cells."""

        header_rows: list[int] = []
        for row_index, row in enumerate(rows):
            cells = cls._row_cells(row)
            if cells and all(cell.kind == "table_header" for cell in cells):
                header_rows.append(row_index)
            else:
                break
        return tuple(header_rows)
