"""Every parser engine must treat a 0-byte source the same way: succeed with
empty output instead of raising.

Text-oriented engines (CSV, Markdown, HTML, plain text, Excel) already get
this for free -- running zero bytes through their normal decode path just
happens to produce empty content. Format-sniffing engines (docx, odt, pptx,
epub, image OCR, JSON) instead raise trying to open/parse an empty stream as
their format ("File is not a zip file", "cannot identify image file",
"Expecting value"). This module pins the same contract across all of them so
a 0-byte file behaves identically regardless of which engine handles it.
"""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers.compat import (
    CsvParser,
    DocxParser,
    EpubParser,
    ExcelParser,
    HtmlParser,
    ImageParser,
    JsonParser,
    MarkdownParser,
    OdtParser,
    PdfParser,
    PptxParser,
    TextParser,
)
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


@pytest.mark.parametrize(
    ("parser", "filename"),
    [
        (DocxParser(), "empty.docx"),
        (OdtParser(), "empty.odt"),
        (PptxParser(), "empty.pptx"),
        (EpubParser(), "empty.epub"),
        (ImageParser(), "empty.png"),
        (JsonParser(), "empty.json"),
        (CsvParser(), "empty.csv"),
        (ExcelParser(), "empty.xlsx"),
        (HtmlParser(), "empty.html"),
        (MarkdownParser(), "empty.md"),
        (TextParser(), "empty.txt"),
    ],
)
def test_engine_succeeds_with_empty_output_for_zero_byte_input(parser, filename):
    document = parser.parse(ParseInput(content=b"", filename=filename))

    assert document.content == ""
    assert document.elements == []
    assert document.warnings is None


def test_pdf_family_returns_empty_content_for_zero_byte_input_without_trying_engines():
    """The PDF family has its own multi-engine fallback router (unlike the
    single-engine formats above), so the empty-input short-circuit lives at
    `HarborPDFParser` instead of on any one engine -- covered here rather than
    parametrized alongside the single-engine parsers."""
    document = PdfParser(backends=[], min_content_chars=10).parse_input(
        ParseInput(content=b"", filename="empty.pdf")
    )

    assert document.content == ""
    assert document.elements == []
    assert document.metadata["pdf_engine"] == "empty-input"
