"""Word ``.docx`` engine that keeps tables as Markdown tables.

Headers, body and footers are read from the package XML in document order, so a
table reaches chunk context as rows and columns instead of one cell per line.
Every part is parsed with defusedxml, never the standard library parser.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from harborrag_adapters._shared.text.markdown_tables import (
    MAX_TABLE_SPAN,
    TableCell,
    markdown_table,
    positive_span,
)
from harborrag_adapters._shared.text.normalization import compact_text
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import (
    guard_input_size,
    open_guarded_zip,
    raise_if_password_protected_document,
    wrap_parse_errors,
)
from harborrag_adapters.parsers.document.base import HarborDocumentEngine
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

parser_logger = get_parser_logger("docx")

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARAGRAPH = f"{_WORD}p"
_TABLE = f"{_WORD}tbl"
_ROW = f"{_WORD}tr"
_CELL = f"{_WORD}tc"
_TEXT = f"{_WORD}t"
_TAB = f"{_WORD}tab"
_LINE_BREAKS = frozenset({f"{_WORD}br", f"{_WORD}cr"})
_BLOCK_CONTAINERS = frozenset(
    {f"{_WORD}body", f"{_WORD}sdt", f"{_WORD}sdtContent", f"{_WORD}customXml"}
)
"""Elements that only wrap paragraphs, tables or rows, such as content controls."""
_VALUE = f"{_WORD}val"
_FIRST_ROW = f"{_WORD}firstRow"
_FIRST_ROW_LOOK_BIT = 0x0020
"""Bit of the legacy hexadecimal ``tblLook`` value that turns the header row on."""
_OFF_VALUES = frozenset({"0", "false", "off"})
_MAIN_PART = "word/document.xml"
_HEADER_PART = re.compile(r"word/header\d*\.xml")
_FOOTER_PART = re.compile(r"word/footer\d*\.xml")

type XmlParser = Callable[[bytes], Element]
type TableRenderer = Callable[[Element], str]


class _VerticalMerge(StrEnum):
    """A cell's part in a vertical merge: none, the first cell, or a covered one."""

    NONE = "none"
    RESTART = "restart"
    CONTINUE = "continue"


@dataclass(frozen=True, slots=True)
class _GridCell:
    """One ``w:tc`` placed on its table's grid.

    Attributes:
        text: Cell text, one line per paragraph.
        column: First grid column the cell covers.
        column_span: Grid columns the cell covers.
        merge: The cell's part in a vertical merge.
    """

    text: str
    column: int
    column_span: int
    merge: _VerticalMerge


class DocxDocumentEngine(HarborDocumentEngine):
    """Extract text from Word ``.docx`` files, keeping tables as Markdown tables.

    The whole package becomes one paragraph element, as with every flat text
    engine. Tables are not separate elements, so each stays inside the text
    around it instead of becoming its own small chunk.

    Attributes:
        parser_name: Parser family name used for routing.
        parser_engine: Engine name recorded in parse metadata and errors.
        parser_version: Version recorded with each parse result.
        suffixes: File suffixes this engine accepts.
        content_types: Media types this engine accepts.
    """

    parser_name: ClassVar[str] = "docx"
    parser_engine: ClassVar[str] = "python/zipfile+defusedxml"
    parser_version: ClassVar[str | None] = "1.1.0"
    suffixes: ClassVar[frozenset[str]] = frozenset({"docx"})
    content_types: ClassVar[frozenset[str]] = frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    )

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Read the header, body and footer text of one ``.docx`` package.

        Args:
            input: The ``.docx`` bytes or path to parse.

        Returns:
            The text as one paragraph element, or an empty result for empty input.

        Raises:
            ParseError: The package is corrupt, encrypted, too large, or holds
                malformed or unsafe XML.
        """
        parse_input = self.coerce_input(input)
        xml_parser, xml_error = _xml_parser()
        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        if not source_bytes:
            return self.empty_result(parse_input)
        parser_logger.debug(
            "Extracting DOCX text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                input_bytes=len(source_bytes),
            ),
        )
        with wrap_parse_errors(self.parser_engine):
            raise_if_password_protected_document(source_bytes, format_name="docx")
            with open_guarded_zip(source_bytes) as archive:
                raise_if_password_protected_document(
                    source_bytes,
                    format_name="docx",
                    archive=archive,
                )
                roots = [
                    self._part_root(archive, part, xml_parser, xml_error)
                    for part in _text_parts(archive.namelist())
                ]
            blocks = [block for root in roots for block in _blocks(root, _markdown_table)]
        content = compact_text("\n\n".join(blocks))
        elements = (
            [
                DocumentElement(
                    id="docx:0",
                    type="paragraph",
                    content=content,
                    metadata={"filename": parse_input.filename},
                )
            ]
            if content
            else []
        )
        parser_logger.info(
            "Parsed DOCX %s content_chars=%d elements=%d",
            input_label(parse_input),
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                content_chars=len(content),
                elements=len(elements),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input),
        )

    def _part_root(
        self,
        archive: zipfile.ZipFile,
        part: str,
        xml_parser: XmlParser,
        xml_error: type[Exception],
    ) -> Element:
        """Parse one package part, turning malformed XML into a ``ParseError``."""
        try:
            return xml_parser(archive.read(part))
        except xml_error as exc:
            raise ParseError(f"{self.parser_engine} found malformed XML in {part}") from exc


DocxParser = DocxDocumentEngine


def _xml_parser() -> tuple[XmlParser, type[Exception]]:
    """Import defusedxml lazily, failing closed when it is not installed."""
    try:
        from defusedxml.ElementTree import ParseError as XmlParseError
        from defusedxml.ElementTree import fromstring
    except ImportError as exc:
        parser_logger.error(
            "DOCX parser dependency `defusedxml` is missing",
            extra=parser_log_extra(parser_name="docx"),
        )
        raise ParseError(
            "DOCX parsing requires `defusedxml`; install `harborrag-adapters[parsers]` "
            "or `pip install defusedxml`."
        ) from exc
    return fromstring, XmlParseError


def _text_parts(names: Sequence[str]) -> list[str]:
    """Return the parts holding text: headers, the main document, then footers."""
    return [
        *(name for name in names if _HEADER_PART.fullmatch(name)),
        _MAIN_PART,
        *(name for name in names if _FOOTER_PART.fullmatch(name)),
    ]


def _blocks(container: Element, render_table: TableRenderer) -> Iterator[str]:
    """Yield the paragraphs and tables of a body, header, footer or cell in order."""
    for child in container:
        if child.tag == _PARAGRAPH:
            text = _paragraph_text(child)
        elif child.tag == _TABLE:
            text = render_table(child)
        elif child.tag in _BLOCK_CONTAINERS:
            yield from _blocks(child, render_table)
            continue
        else:
            continue
        if text:
            yield text


def _paragraph_text(paragraph: Element) -> str:
    """Join a paragraph's runs, with tabs, line breaks and text-box paragraphs."""
    parts: list[str] = []
    for node in paragraph.iter():
        if node is paragraph:
            continue
        if node.tag == _TEXT:
            parts.append(node.text or "")
        elif node.tag == _TAB:
            parts.append("\t")
        elif node.tag in _LINE_BREAKS or node.tag == _PARAGRAPH:
            parts.append("\n")
    return "".join(parts).strip()


def _markdown_table(table: Element) -> str:
    """Render one top-level table as a Markdown table, merged cells shown once."""
    rows = _grid_rows(table)
    header = _has_header_row(table)
    continued = [
        {cell.column for cell in row if cell.merge is _VerticalMerge.CONTINUE} for row in rows
    ]
    covered: set[tuple[int, int]] = set()
    rendered: list[list[TableCell]] = []
    for row_index, row in enumerate(rows):
        cells: list[TableCell] = []
        for cell in row:
            if (row_index, cell.column) in covered:
                continue
            row_span = (
                _row_span(continued, row_index, cell.column)
                if cell.merge is _VerticalMerge.RESTART
                else 1
            )
            covered.update((row_index + offset, cell.column) for offset in range(1, row_span))
            cells.append(
                TableCell(
                    text=cell.text,
                    row_span=row_span,
                    column_span=cell.column_span,
                    header=header and row_index == 0,
                )
            )
        rendered.append(cells)
    return markdown_table(rendered)


def _plain_table(table: Element) -> str:
    """Render a table nested in a cell as one line of cell text per row."""
    lines = (" ".join(cell.text for cell in row if cell.text) for row in _grid_rows(table))
    return "\n".join(line for line in lines if line)


def _grid_rows(table: Element) -> list[list[_GridCell]]:
    """Place each row's cells on the table grid, keeping their merge markers."""
    rows: list[list[_GridCell]] = []
    for row in _children(table, _ROW):
        cells = [
            _GridCell(text="", column=column, column_span=1, merge=_VerticalMerge.NONE)
            for column in range(_omitted_leading_columns(row))
        ]
        column = len(cells)
        for cell in _children(row, _CELL):
            column_span = positive_span(_cell_property(cell, "gridSpan"))
            cells.append(
                _GridCell(
                    text="\n".join(_blocks(cell, _plain_table)),
                    column=column,
                    column_span=column_span,
                    merge=_vertical_merge(cell),
                )
            )
            column += column_span
        rows.append(cells)
    return rows


def _children(element: Element, tag: str) -> Iterator[Element]:
    """Yield the ``tag`` children of an element, looking through wrappers."""
    for child in element:
        if child.tag == tag:
            yield child
        elif child.tag in _BLOCK_CONTAINERS:
            yield from _children(child, tag)


def _omitted_leading_columns(row: Element) -> int:
    """Count the grid columns Word leaves empty before a row's first cell.

    The count comes from ``w:gridBefore`` and is capped at ``MAX_TABLE_SPAN``,
    like any other span read from untrusted markup.
    """
    grid_before = row.find(f"{_WORD}trPr/{_WORD}gridBefore")
    if grid_before is None:
        return 0
    try:
        return min(max(int(grid_before.get(_VALUE) or "0"), 0), MAX_TABLE_SPAN)
    except ValueError:
        return 0


def _row_span(continued: list[set[int]], row_index: int, column: int) -> int:
    """Count the rows a vertical merge starting at this cell covers."""
    span = 1
    for columns in continued[row_index + 1 :]:
        if column not in columns:
            break
        span += 1
    return span


def _vertical_merge(cell: Element) -> _VerticalMerge:
    merge = cell.find(f"{_WORD}tcPr/{_WORD}vMerge")
    if merge is None:
        return _VerticalMerge.NONE
    if merge.get(_VALUE) == _VerticalMerge.RESTART.value:
        return _VerticalMerge.RESTART
    return _VerticalMerge.CONTINUE


def _cell_property(cell: Element, name: str) -> str | None:
    node = cell.find(f"{_WORD}tcPr/{_WORD}{name}")
    return None if node is None else node.get(_VALUE)


def _has_header_row(table: Element) -> bool:
    """Report whether Word shows the table's first row as its header row.

    The row's own ``tblHeader`` mark wins. Otherwise the table's ``tblLook``
    decides, through its ``firstRow`` attribute or the legacy hexadecimal value.
    """
    first_row = next(_children(table, _ROW), None)
    if first_row is None:
        return False
    repeat = first_row.find(f"{_WORD}trPr/{_WORD}tblHeader")
    if repeat is not None and _is_on(repeat.get(_VALUE)):
        return True
    look = table.find(f"{_WORD}tblPr/{_WORD}tblLook")
    if look is None:
        return False
    first_row_flag = look.get(_FIRST_ROW)
    if first_row_flag is not None:
        return _is_on(first_row_flag)
    return bool(_look_bits(look.get(_VALUE)) & _FIRST_ROW_LOOK_BIT)


def _is_on(value: str | None) -> bool:
    """Read a Word on/off value, where a missing value means on."""
    return value is None or value.strip().casefold() not in _OFF_VALUES


def _look_bits(value: str | None) -> int:
    try:
        return int(value or "0", 16)
    except ValueError:
        return 0
