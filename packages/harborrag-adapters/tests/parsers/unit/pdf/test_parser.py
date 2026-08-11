"""Unit tests for PDF-family fallback and quality-profile construction."""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_adapters.parsers.compat import (
    PARSER_LOGGER_NAME,
    PdfBackend,
    PdfParser,
    PdfParseResult,
    PdfParserProfile,
)
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit


class EmptyPdfBackend(PdfBackend):
    name: ClassVar[str] = "empty_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        return PdfParseResult(content="", engine=self.name)


class PageLimitRejectingPdfBackend(PdfBackend):
    """Mimics an engine rejecting a document for exceeding a configured limit
    (e.g. pymupdf's `max_pages` or docling's `max_file_size`) instead of
    finding it unparseable."""

    name: ClassVar[str] = "limited_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        from harborrag_adapters.parsers.errors import ParseError

        raise ParseError("PDF has 10 pages, exceeding the max_pages=1 cap")


class UsefulPdfBackend(PdfBackend):
    name: ClassVar[str] = "useful_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        content = "Useful PDF content for downstream retrieval"
        return PdfParseResult(
            content=content,
            engine=self.name,
            elements=[DocumentElement(id="pdf:useful:0", type="paragraph", content=content)],
            metadata={"page_count": 1},
        )


@pytest.mark.graybox
def test_pdf_parser_falls_back_until_backend_returns_usable_content(caplog):
    caplog.set_level(logging.DEBUG, logger=PARSER_LOGGER_NAME)
    parser = PdfParser(
        backends=[EmptyPdfBackend(), UsefulPdfBackend()],
        min_content_chars=10,
    )

    document = parser.parse_input(ParseInput(content=b"%PDF", filename="doc.pdf"))

    assert document.content == "Useful PDF content for downstream retrieval"
    assert document.metadata["pdf_engine"] == "useful_pdf"
    assert document.metadata["page_count"] == 1
    assert document.warnings == [
        "empty_pdf: extracted 0 characters; requires 10",
    ]
    assert any(
        record.harbor_parser_engine == "empty_pdf"
        for record in caplog.records
        if hasattr(record, "harbor_parser_engine")
    )


class ExplodingPdfBackend(PdfBackend):
    """Would fail like every real engine does on a 0-byte file (can't open an
    empty stream) -- used to prove the empty-input short-circuit means no
    engine is ever invoked for a genuinely empty source."""

    name: ClassVar[str] = "exploding_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        raise AssertionError("engine should not run for a 0-byte PDF input")


@pytest.mark.graybox
def test_pdf_parser_returns_empty_content_for_zero_byte_input_instead_of_failing():
    """A 0-byte PDF has nothing for any engine to parse; every real engine
    raises trying to open it ("cannot open empty stream", "invalid PDF
    format", ...), which used to bubble up as a hard `PDFParsingFailedError`
    instead of a graceful empty result."""
    parser = PdfParser(backends=[ExplodingPdfBackend()], min_content_chars=10)

    document = parser.parse_input(ParseInput(content=b"", filename="empty.pdf"))

    assert document.content == ""
    assert document.elements == []
    assert document.metadata["pdf_engine"] == "empty-input"
    assert document.warnings is None


@pytest.mark.graybox
def test_pdf_parser_rejects_oversized_input_before_any_engine_runs(monkeypatch):
    """The size ceiling used to be applied by pymupdf alone; the other four
    real PDF engines had no size check at all, and profile ordering (`ocr`,
    `quality`, `scientific`) can put an unguarded engine first. The guard
    must now apply before engine selection, regardless of which engine
    would have run -- proven here with a fake engine that fails the test if
    invoked, matching the file's other "no engine runs" tests."""
    from harborrag_adapters.parsers.errors import ParseError
    from harborrag_adapters.parsers.pdf import parser_support

    def _always_too_big(*_args: object, **_kwargs: object) -> None:
        raise ParseError("Input size 999999999 exceeds max_input_bytes 0")

    monkeypatch.setattr(parser_support, "guard_parse_input_size", _always_too_big)
    parser = PdfParser(backends=[ExplodingPdfBackend()], min_content_chars=10)

    with pytest.raises(ParseError):
        parser.parse_input(ParseInput(content=b"%PDF-1.4 not actually empty", filename="big.pdf"))


@pytest.mark.graybox
def test_pdf_parsing_failed_error_names_the_rejection_reason_per_engine():
    """When every engine in the chain rejects a document, the raised error
    must say *why* each one did -- not just list engine names. A page/size
    limit rejection (docling's max_file_size, pymupdf's max_pages) otherwise
    looks like an unexplained generic parsing failure."""
    from harborrag_adapters.parsers.errors import PDFParsingFailedError

    parser = PdfParser(
        backends=[PageLimitRejectingPdfBackend(), EmptyPdfBackend()],
        min_content_chars=10,
    )

    with pytest.raises(PDFParsingFailedError) as excinfo:
        parser.parse_input(ParseInput(content=b"%PDF", filename="doc.pdf"))

    message = str(excinfo.value)
    assert "limited_pdf" in message
    assert "exceeding the max_pages=1 cap" in message
    assert "empty_pdf" in message


class NoTextPdfBackend(PdfBackend):
    """Mimics a non-OCR engine finding pages but no text layer at all."""

    name: ClassVar[str] = "no_text_pdf_a"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        from harborrag_adapters.parsers.errors import NoExtractableTextError

        raise NoExtractableTextError(page_count=3)


class AlsoNoTextPdfBackend(NoTextPdfBackend):
    """A second, independent engine that agrees: no extractable text."""

    name: ClassVar[str] = "no_text_pdf_b"


@pytest.mark.graybox
def test_pdf_parser_raises_shared_typed_cause_when_every_engine_agrees():
    """When every configured engine independently rejects a document for the
    *same* typed reason (here: no extractable text), the caller should get
    that specific typed error directly -- not the generic
    `PDFParsingFailedError` aggregate, which can't be caught to distinguish
    "needs OCR" from any other kind of rejection."""
    from harborrag_adapters.parsers.errors import NoExtractableTextError

    parser = PdfParser(
        backends=[NoTextPdfBackend(), AlsoNoTextPdfBackend()],
        min_content_chars=10,
    )

    with pytest.raises(NoExtractableTextError) as excinfo:
        parser.parse_input(ParseInput(content=b"%PDF", filename="scan.pdf"))

    assert excinfo.value.page_count == 3


@pytest.mark.whitebox
def test_pdf_quality_profile_builds_expected_advanced_backends():
    backends = HarborParserFactory().create_pdf_parser(profile=PdfParserProfile.QUALITY).backends

    assert [backend.name for backend in backends] == [
        "docling",
        "mineru",
        "paddleocr",
        "pymupdf",
        "liteparse",
    ]
    assert backends[0].options.do_ocr is True
    assert backends[0].options.do_table_structure is True
    assert backends[1].options.backend == "hybrid"
    assert backends[1].options.effort == "medium"
    assert backends[2].options.use_formula_recognition is True
    assert backends[2].options.use_region_detection is True
