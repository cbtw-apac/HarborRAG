"""Unit tests for PdfParser backend fallback and quality-profile construction."""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest
from harborrag_adapters.parsers import (
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

    def parse(self, input: ParseInput) -> PdfParseResult:
        return PdfParseResult(content="", engine=self.name)


class UsefulPdfBackend(PdfBackend):
    name: ClassVar[str] = "useful_pdf"

    def parse(self, input: ParseInput) -> PdfParseResult:
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

    document = parser.parse(ParseInput(content=b"%PDF", filename="doc.pdf"))

    assert document.content == "Useful PDF content for downstream retrieval"
    assert document.metadata["pdf_engine"] == "useful_pdf"
    assert document.metadata["page_count"] == 1
    assert document.warnings == [
        "empty_pdf: extracted less than 10 characters",
    ]
    assert any(
        record.harbor_parser_engine == "empty_pdf"
        for record in caplog.records
        if hasattr(record, "harbor_parser_engine")
    )


@pytest.mark.whitebox
def test_pdf_quality_profile_builds_expected_advanced_backends():
    backends = PdfParser.default_backends(PdfParserProfile.QUALITY)

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
