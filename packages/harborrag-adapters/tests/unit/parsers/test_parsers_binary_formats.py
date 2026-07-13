"""White-box unit tests for the docx/pptx/excel/epub parsers."""

from __future__ import annotations

import pytest
from harbor_test_builders import build_epub_bytes, build_pptx_bytes, build_xlsx_bytes
from harborrag_adapters.parsers import DocxParser, EpubParser, ExcelParser, PptxParser
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_docx_parser_extracts_text(docx_bytes):
    document = DocxParser().parse(
        ParseInput(content=docx_bytes, filename="report.docx")
    )
    assert document.parser_name == "docx"
    assert "Hello Harbor" in document.content
    assert document.metadata["filename"] == "report.docx"


def test_pptx_parser_extracts_slide_text_and_count():
    document = PptxParser().parse(
        ParseInput(content=build_pptx_bytes("Kickoff slide"), filename="deck.pptx")
    )
    assert "Kickoff slide" in document.content
    assert document.metadata["slide_count"] == 1
    assert document.elements[0].metadata["slide"] == 1


def test_excel_parser_extracts_sheet_text_and_names():
    xlsx = build_xlsx_bytes([["header"], ["value-one"], ["value-two"]])
    document = ExcelParser().parse(ParseInput(content=xlsx, filename="book.xlsx"))
    assert document.parser_name == "excel"
    assert "value-one" in document.content
    assert "value-two" in document.content
    assert document.metadata["sheets"] == ["Sheet1"]
    assert document.elements[0].metadata["sheet"] == "Sheet1"


def test_fixture_builders_preserve_explicit_empty_collections():
    import io
    import zipfile

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(build_xlsx_bytes([])), read_only=True)
    assert list(workbook.active.values) == []

    epub = build_epub_bytes([])
    with zipfile.ZipFile(io.BytesIO(epub)) as archive:
        assert not any(name.endswith(".xhtml") for name in archive.namelist())
        assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


def test_epub_fixture_builder_escapes_xhtml_section_text():
    import io
    import zipfile

    epub = build_epub_bytes(["A & <B>"])
    with zipfile.ZipFile(io.BytesIO(epub)) as archive:
        chapter = archive.read("OEBPS/ch1.xhtml").decode("utf-8")

    assert "A &amp; &lt;B&gt;" in chapter


def test_excel_parser_advertises_legacy_xls_route():
    assert ExcelParser().can_parse(ParseInput(content=b"x", filename="legacy.xls"))


def test_epub_parser_preserves_spine_section_order():
    epub = build_epub_bytes(["Alpha section", "Beta section", "Gamma section"])
    document = EpubParser().parse(ParseInput(content=epub, filename="b.epub"))

    assert document.content == "Alpha section\n\nBeta section\n\nGamma section"
    assert [element.metadata["order"] for element in document.elements] == [1, 2, 3]
    assert document.metadata["sections"] == 3
    assert document.warnings is None


def test_epub_parser_warns_on_missing_referenced_section():
    import io
    import zipfile

    original = build_epub_bytes(["One text", "Two text"])
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(original)) as source,
        zipfile.ZipFile(buffer, "w") as sink,
    ):
        for info in source.infolist():
            if info.filename == "OEBPS/ch2.xhtml":
                continue  # spine still references it -> should warn, not crash
            sink.writestr(info, source.read(info.filename))

    document = EpubParser().parse(
        ParseInput(content=buffer.getvalue(), filename="b.epub")
    )
    assert document.content == "One text"
    assert document.warnings is not None
    assert any("ch2.xhtml" in warning for warning in document.warnings)
    assert [element.metadata["order"] for element in document.elements] == [1]
