from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harborrag_core.chunking import (
    CanonicalIdentityBuilder,
    SourceLocator,
    content_fingerprint,
    encoded_identifier,
)
from harborrag_core.domain import (
    DocumentElement,
    TableArtifact,
    TableCell,
    TableCellType,
    TableGridSlot,
)


@dataclass(frozen=True, slots=True)
class _TableBuildContext:
    document_id: str
    document_version_id: str
    source_version: str
    source_url: str


class FlatTableArtifactBuilder:
    """Build canonical table topology from parser-owned tabular elements."""

    def __init__(
        self,
        identity: CanonicalIdentityBuilder | None = None,
    ) -> None:
        self._identity = identity or CanonicalIdentityBuilder()

    def build(
        self,
        elements: Sequence[DocumentElement],
        *,
        document_id: str,
        document_version_id: str,
        source_version: str,
        source_url: str,
    ) -> tuple[TableArtifact, ...]:
        headings: list[str] = []
        artifacts: list[TableArtifact] = []
        context = _TableBuildContext(
            document_id=document_id,
            document_version_id=document_version_id,
            source_version=source_version,
            source_url=source_url,
        )
        for element in elements:
            if element.type == "heading":
                self._update_headings(headings, element)
            elif element.type == "table":
                artifacts.append(
                    self._table(
                        element,
                        ordinal=len(artifacts),
                        section_path=tuple(headings),
                        context=context,
                    )
                )
        return tuple(artifacts)

    def _table(
        self,
        element: DocumentElement,
        *,
        ordinal: int,
        section_path: tuple[str, ...],
        context: _TableBuildContext,
    ) -> TableArtifact:
        rows = self._rows(element)
        column_count = max(map(len, rows))
        normalized = tuple((*row, *("" for _ in range(column_count - len(row)))) for row in rows)
        table_id = self._identity.table_id(
            document_id=context.document_id,
            section_path=section_path,
            stable_table_location={
                "source_element_id": element.id,
                "ordinal": ordinal,
            },
        )
        cells, grid = self._cells(table_id, normalized)
        header_rows = self._header_rows(element.metadata, len(rows))
        column_names = tuple(
            value or f"Column {index + 1}" for index, value in enumerate(normalized[0])
        )
        content_hash = content_fingerprint(element.content or "")
        return TableArtifact(
            table_id=table_id,
            table_version_id=self._identity.table_version_id(
                table_id=table_id,
                source_version=context.source_version,
                content_hash=content_hash,
            ),
            document_id=context.document_id,
            document_version_id=context.document_version_id,
            source_version=context.source_version,
            source_block_id=element.id,
            ordinal=ordinal,
            caption=self._optional_text(element.metadata.get("caption")),
            section_path=section_path,
            row_count=len(normalized),
            column_count=column_count,
            header_row_indices=header_rows,
            column_names=column_names,
            header_hierarchy=tuple(((name,) if name else ()) for name in column_names),
            cells=cells,
            logical_grid=grid,
            source_locator=self._source_locator(
                context.source_url,
                element,
            ),
            content_hash=content_hash,
        )

    @staticmethod
    def _rows(element: DocumentElement) -> tuple[tuple[str, ...], ...]:
        rows = tuple(
            tuple(cell.strip() for cell in line.split("\t"))
            for line in (element.content or "").splitlines()
            if line.strip()
        )
        if not rows or max(map(len, rows)) < 1:
            raise ValueError(f"table element has no cells: {element.id}")
        return rows

    @staticmethod
    def _cells(
        table_id: str,
        rows: tuple[tuple[str, ...], ...],
    ) -> tuple[
        tuple[TableCell, ...],
        tuple[tuple[TableGridSlot, ...], ...],
    ]:
        cells: list[TableCell] = []
        grid: list[tuple[TableGridSlot, ...]] = []
        for row_index, row in enumerate(rows):
            slots = []
            for column_index, text in enumerate(row):
                cell_id = encoded_identifier(
                    "table-cell",
                    {
                        "table_id": table_id,
                        "row": row_index,
                        "column": column_index,
                    },
                )
                cells.append(
                    TableCell(
                        cell_id=cell_id,
                        row_index=row_index,
                        column_index=column_index,
                        text=text,
                        value=text or None,
                        cell_type=(
                            TableCellType.HEADER
                            if row_index == 0
                            else (TableCellType.EMPTY if not text else TableCellType.TEXT)
                        ),
                        is_header=row_index == 0,
                    )
                )
                slots.append(TableGridSlot(cell_id=cell_id))
            grid.append(tuple(slots))
        return tuple(cells), tuple(grid)

    @staticmethod
    def _header_rows(
        metadata: Mapping[str, object],
        row_count: int,
    ) -> tuple[int, ...]:
        value = metadata.get("header_rows", 1 if row_count > 1 else 0)
        count = value if isinstance(value, int) and not isinstance(value, bool) else 0
        return tuple(range(min(max(count, 0), row_count)))

    @staticmethod
    def _source_locator(
        source_url: str,
        element: DocumentElement,
    ) -> SourceLocator:
        metadata = element.metadata
        start_line = FlatTableArtifactBuilder._positive_int(metadata.get("start_line"))
        end_line = FlatTableArtifactBuilder._positive_int(metadata.get("end_line"))
        if (start_line is None) != (end_line is None):
            start_line = None
            end_line = None
        return SourceLocator(
            uri=source_url,
            start_line=start_line,
            end_line=end_line,
            source_element_ids=(element.id,),
        )

    @staticmethod
    def _positive_int(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    @staticmethod
    def _update_headings(
        headings: list[str],
        element: DocumentElement,
    ) -> None:
        title = (element.content or "").strip()
        if not title:
            return
        value = element.metadata.get("level", 1)
        level = value if isinstance(value, int) and not isinstance(value, bool) else 1
        level = min(max(level, 1), 6)
        del headings[level - 1 :]
        headings.append(title)

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return str(value).strip() if value is not None and str(value).strip() else None
