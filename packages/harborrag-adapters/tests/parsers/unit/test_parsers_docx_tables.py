from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import pytest

from harborrag_adapters.parsers.compat import DocxParser
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.domain.parser import ParsedDocument, ParseInput

if TYPE_CHECKING:
    from docx.document import Document as WordDocument
    from docx.table import Table

pytestmark = pytest.mark.unit

_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def test_docx_table_renders_as_markdown_table_between_its_paragraphs() -> None:
    document = _document()
    document.add_paragraph("Suggested validation points:")
    _add_table(
        document,
        [
            ["Check Area", "Expected Result", "Evidence Note"],
            ["Upload", "Completed successfully", "Use key AUTO-KEY-1"],
            ["Preview", "Readable in English", "Page 05 content visible"],
        ],
    )
    document.add_paragraph("Closing note.")

    parsed = _parse(document)

    assert parsed.content == (
        "Suggested validation points:\n\n"
        "| Check Area | Expected Result | Evidence Note |\n"
        "| --- | --- | --- |\n"
        "| Upload | Completed successfully | Use key AUTO-KEY-1 |\n"
        "| Preview | Readable in English | Page 05 content visible |\n\n"
        "Closing note."
    )
    assert [(element.id, element.type) for element in parsed.elements] == [("docx:0", "paragraph")]
    assert parsed.elements[0].content == parsed.content


def test_docx_table_without_header_row_keeps_first_row_as_data() -> None:
    document = _document()
    table = _add_table(document, [["Owner", "Alex Example"]])
    table._tbl.tblPr.find(f"{_WORD_NAMESPACE}tblLook").set(f"{_WORD_NAMESPACE}firstRow", "0")

    parsed = _parse(document)

    assert parsed.content == "| Column 1 | Column 2 |\n| --- | --- |\n| Owner | Alex Example |"


def test_docx_merged_cells_are_shown_once_with_their_spans() -> None:
    document = _document()
    table = _add_table(document, [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]])
    table.cell(1, 0).merge(table.cell(1, 1))
    table.cell(1, 2).merge(table.cell(2, 2))

    parsed = _parse(document)

    assert parsed.content.splitlines() == [
        "| A | B | C |",
        "| --- | --- | --- |",
        "| 1<br>2 (spans 2 columns) |  | 3<br>6 (spans 2 rows) |",
        "| 4 | 5 |  |",
    ]


def test_docx_header_and_footer_text_surround_the_body() -> None:
    document = _document()
    document.sections[0].header.paragraphs[0].text = "Header line"
    document.sections[0].footer.paragraphs[0].text = "Footer line"
    document.add_paragraph("Body line")

    parsed = _parse(document)

    assert parsed.content == "Header line\n\nBody line\n\nFooter line"


@pytest.mark.parametrize(
    "document_xml",
    [
        b"<w:document",
        b'<!DOCTYPE d [<!ENTITY e "boom">]><d>&e;</d>',
    ],
    ids=["truncated", "entity-declaration"],
)
def test_docx_with_unsafe_or_malformed_xml_raises_parse_error(document_xml: bytes) -> None:
    document = _document()
    document.add_paragraph("Body line")
    package = _replace_part(_save(document), "word/document.xml", document_xml)

    with pytest.raises(ParseError):
        DocxParser().parse(ParseInput(content=package, filename="broken.docx"))


def _document() -> WordDocument:
    from docx import Document

    return Document()


def _add_table(document: WordDocument, rows: list[list[str]]) -> Table:
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for row_index, row in enumerate(rows):
        for column_index, text in enumerate(row):
            table.cell(row_index, column_index).text = text
    return table


def _save(document: WordDocument) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _parse(document: WordDocument) -> ParsedDocument:
    return DocxParser().parse(ParseInput(content=_save(document), filename="checks.docx"))


def _replace_part(package: bytes, name: str, data: bytes) -> bytes:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(package)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            target.writestr(item, data if item.filename == name else source.read(item))
    return output.getvalue()
