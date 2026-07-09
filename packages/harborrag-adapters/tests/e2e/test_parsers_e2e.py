from __future__ import annotations

import pytest

from harborrag_adapters.parsers import (
    HarborParser,
    ParseError,
    UnsupportedFormatError,
)
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput


@pytest.fixture
def parser() -> HarborParser:
    return HarborParser()


def _assert_parsed(document: ParsedDocument, expected_parser: str) -> None:
    """Common structural assertions for any successfully parsed document."""
    assert isinstance(document, ParsedDocument)
    assert document.parser_name == expected_parser
    assert isinstance(document.content, str)
    # Elements are optional in the schema, but when present must be the domain type.
    if document.elements is not None:
        assert isinstance(document.elements, list)
        assert all(isinstance(el, DocumentElement) for el in document.elements)


# ---------------------------------------------------------------------------
# Text-ish formats parsed from in-memory content
# ---------------------------------------------------------------------------

TEXT_CASES = [
    ("note.txt", "plain text body that is long enough", "text"),
    ("doc.md", "# Title\n\nSome markdown body paragraph.", "markdown"),
    ("page.html", "<html><body><p>Visible HTML text</p></body></html>", "html"),
    ("data.csv", "name,role\nAda,engineer\nGrace,admiral", "csv"),
    ("payload.json", '{"key": "value", "n": 1}', "json"),
]


@pytest.mark.parametrize("filename,content,expected", TEXT_CASES)
def test_parse_text_formats_end_to_end(parser, filename, content, expected):
    document = parser.parse(ParseInput(content=content, filename=filename))

    _assert_parsed(document, expected)
    assert document.content  # non-empty extraction for all text formats
    assert document.elements  # each text parser emits at least one element


# ---------------------------------------------------------------------------
# Binary office / ebook formats built from the shared fixtures
# ---------------------------------------------------------------------------

def test_parse_docx_end_to_end(parser, docx_bytes):
    document = parser.parse(ParseInput(content=docx_bytes, filename="a.docx"))
    _assert_parsed(document, "docx")
    assert "Hello Harbor" in document.content


def test_parse_pptx_end_to_end(parser, pptx_bytes):
    document = parser.parse(ParseInput(content=pptx_bytes, filename="a.pptx"))
    _assert_parsed(document, "pptx")
    assert "Slide text" in document.content


def test_parse_xlsx_end_to_end(parser, xlsx_bytes):
    document = parser.parse(ParseInput(content=xlsx_bytes, filename="a.xlsx"))
    _assert_parsed(document, "excel")
    assert "Ada" in document.content


def test_parse_epub_end_to_end(parser, epub_bytes):
    document = parser.parse(ParseInput(content=epub_bytes, filename="a.epub"))
    _assert_parsed(document, "epub")
    assert "Chapter one text" in document.content
    assert document.elements  # one element per section


def test_parse_png_ocr_end_to_end(parser, png_bytes):
    """Image OCR is optional: the tesseract binary may be absent in CI.

    We assert the public contract either succeeds (content is a str) or fails
    with the normalized :class:`ParseError`, and skip when OCR is unavailable.
    """
    try:
        document = parser.parse(ParseInput(content=png_bytes, filename="a.png"))
    except ParseError as exc:
        pytest.skip(f"image OCR unavailable: {exc}")
    else:
        _assert_parsed(document, "image")
        assert isinstance(document.content, str)


# ---------------------------------------------------------------------------
# Routing behaviour
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content_type,content,expected",
    [
        ("text/html", "<p>Hello content-type routing</p>", "html"),
        ("application/json", '{"routed": "by content type"}', "json"),
        ("text/csv", "name,role\nAda,eng", "csv"),
    ],
)
def test_routing_by_content_type_only(parser, content_type, content, expected):
    """Routing must work from content_type alone, with no filename/suffix."""
    document = parser.parse(ParseInput(content=content, content_type=content_type))
    assert document.parser_name == expected


@pytest.mark.parametrize("filename,expected", [(f, e) for f, _, e in TEXT_CASES])
def test_parser_for_returns_expected_parser(parser, filename, expected):
    selected = parser.parser_for(ParseInput(content="x", filename=filename))
    assert selected is not None
    assert selected.name == expected


def test_parser_for_unknown_returns_none(parser):
    assert parser.parser_for(ParseInput(content=b"\x00\x01", filename="a.zzz")) is None


def test_parse_unknown_format_raises(parser):
    with pytest.raises(UnsupportedFormatError):
        parser.parse(ParseInput(content=b"\x00\x01\x02", filename="mystery.zzz"))


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename,content,_expected", TEXT_CASES)
def test_parse_is_deterministic_text(parser, filename, content, _expected):
    first = parser.parse(ParseInput(content=content, filename=filename))
    second = parser.parse(ParseInput(content=content, filename=filename))
    assert first.content == second.content


def test_parse_is_deterministic_docx(parser, docx_bytes):
    first = parser.parse(ParseInput(content=docx_bytes, filename="a.docx"))
    second = parser.parse(ParseInput(content=docx_bytes, filename="a.docx"))
    assert first.content == second.content


# ---------------------------------------------------------------------------
# PDF end-to-end via a real one-page document
# ---------------------------------------------------------------------------

def test_parse_pdf_end_to_end(parser):
    fitz = pytest.importorskip("fitz", reason="PyMuPDF (fitz) not installed")

    doc = fitz.open()
    page = doc.new_page()
    # The PyMuPDF backend rejects extractions under 20 chars as "not usable",
    # so insert a sentence comfortably above that threshold.
    page.insert_text((72, 72), "Hello PDF this is a longer sentence for extraction.")
    data = doc.tobytes()

    try:
        document = parser.parse(ParseInput(content=data, filename="d.pdf"))
    except ParseError as exc:
        pytest.skip(f"no usable PDF backend available: {exc}")

    _assert_parsed(document, "pdf")
    assert "Hello PDF" in document.content
