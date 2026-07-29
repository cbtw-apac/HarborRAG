"""White-box unit tests for HarborParserEngine routing (can_parse, __init_subclass__)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from harborrag_adapters.parsers.common.base import HarborParserEngine
from harborrag_adapters.parsers.common.mime import normalize_suffix
from harborrag_adapters.parsers.document.engines.docx.engine import DocxDocumentEngine
from harborrag_adapters.parsers.document.engines.epub.engine import EpubDocumentEngine
from harborrag_adapters.parsers.image.engines.ocr.engine import OcrImageEngine
from harborrag_adapters.parsers.markup.engines.html.engine import HtmlMarkupEngine
from harborrag_adapters.parsers.markup.engines.markdown.engine import MarkdownMarkupEngine
from harborrag_adapters.parsers.presentation.engines.python_pptx.engine import (
    PythonPptxPresentationEngine,
)
from harborrag_adapters.parsers.spreadsheet.engines.csv.engine import CsvSpreadsheetEngine
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.engine import ExcelSpreadsheetEngine
from harborrag_adapters.parsers.structured.engines.json.engine import JsonStructuredEngine
from harborrag_adapters.parsers.text.engines.plain_text.engine import PlainTextEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    ("parser", "filename", "content_type"),
    [
        (PlainTextEngine(), "notes.txt", None),
        (PlainTextEngine(), None, "text/plain"),
        (MarkdownMarkupEngine(), "doc.md", None),
        (MarkdownMarkupEngine(), None, "text/markdown"),
        (HtmlMarkupEngine(), "page.html", None),
        (HtmlMarkupEngine(), None, "text/html"),
        (CsvSpreadsheetEngine(), "table.csv", None),
        (CsvSpreadsheetEngine(), "table.tsv", None),
        (CsvSpreadsheetEngine(), None, "text/csv"),
        (JsonStructuredEngine(), "data.json", None),
        (JsonStructuredEngine(), None, "application/json"),
        (DocxDocumentEngine(), "report.docx", None),
        (PythonPptxPresentationEngine(), "deck.pptx", None),
        (ExcelSpreadsheetEngine(), "sheet.xlsx", None),
        (OcrImageEngine(), "pic.png", None),
        (OcrImageEngine(), None, "image/png"),
        (EpubDocumentEngine(), "book.epub", None),
    ],
)
def test_can_parse_matches_advertised_routes(parser, filename, content_type):
    assert parser.can_parse(ParseInput(content=b"x", filename=filename, content_type=content_type))


@pytest.mark.parametrize(
    "parser",
    [
        MarkdownMarkupEngine(),
        CsvSpreadsheetEngine(),
        JsonStructuredEngine(),
        DocxDocumentEngine(),
        OcrImageEngine(),
    ],
)
def test_can_parse_rejects_unrelated_input(parser):
    assert not parser.can_parse(
        ParseInput(content=b"x", filename="mystery.zzz", content_type="x/y")
    )


def test_can_parse_uses_normalized_content_type_parameters():
    # Content type with parameters is normalized before comparison.
    assert HtmlMarkupEngine().can_parse(
        ParseInput(content=b"x", content_type="text/html; charset=utf-8")
    )


def test_init_subclass_normalizes_suffix_and_content_type_declarations():
    class WeirdParser(HarborParserEngine[ParseInput, ParsedDocument]):
        parser_name: ClassVar[str] = "weird"
        # Mixed case, missing dots, surrounding whitespace, and an empty entry.
        suffixes: ClassVar[frozenset[str]] = frozenset({"FOO", ".Bar", " baz "})
        content_types: ClassVar[frozenset[str]] = frozenset(
            {"Application/X-Weird", "  ", "TEXT/Thing "}
        )

        def parse(self, input: ParseInput) -> ParsedDocument:  # pragma: no cover
            raise NotImplementedError

    assert WeirdParser.suffixes == frozenset({".foo", ".bar", ".baz"})
    assert WeirdParser.content_types == frozenset({"application/x-weird", "text/thing"})


def test_normalize_suffix_helper():
    assert normalize_suffix("TXT") == ".txt"
    assert normalize_suffix(".MD") == ".md"
    assert normalize_suffix("  Json ") == ".json"
    assert normalize_suffix("") == ""
