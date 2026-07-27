"""Architecture tests for parser-family routing and PDF provider fallback."""

from __future__ import annotations

from typing import ClassVar

import pytest

import harborrag_adapters.parsers as parsers
from harborrag_adapters.parsers import (
    HarborParserFactory,
    ParseRequest,
)
from harborrag_adapters.parsers.common.config import ParserConfig
from harborrag_adapters.parsers.errors import DuplicatePDFEngineError
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.config import (
    PDFParserConfig,
    PDFParserProfile,
    PDFProfileConfig,
    PDFRouterConfig,
)
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_adapters.parsers.pdf.parser import HarborPDFParser
from harborrag_adapters.parsers.pdf.router import PDFEngineRegistry
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit


class _ResultEngine(HarborPDFEngine):
    name: ClassVar[str] = "result"

    def __init__(self, content: str, quality_score: float | None = None) -> None:
        self._content = content
        self._quality_score = quality_score

    def parse_input(self, input: ParseInput) -> PDFParseResult:
        return PDFParseResult(
            content=self._content,
            engine=self.name,
            quality_score=self._quality_score,
        )


class _SecondResultEngine(_ResultEngine):
    name: ClassVar[str] = "second"


def test_root_package_exposes_only_stable_family_api() -> None:
    assert parsers.__all__ == [
        "HarborParserFactory",
        "HarborParserRegistry",
        "ParseRequest",
        "ParseResult",
    ]
    assert not hasattr(parsers, "DoclingPDFEngine")


def test_default_registry_resolves_complete_parser_families() -> None:
    registry = HarborParserFactory().create_registry()

    assert registry.resolve("report.pdf", "application/pdf").parser_name == "pdf"
    assert registry.resolve("financials.xlsx", None).parser_name == "spreadsheet"
    assert registry.resolve("readme.md", "text/markdown").parser_name == "markup"
    assert registry.resolve("payload.json", None).parser_name == "structured"

    with pytest.raises(ValueError, match="Unknown parser family"):
        registry.create("docling")


def test_factory_applies_custom_pdf_profile_order() -> None:
    router_config = PDFRouterConfig(
        default_profile="research",
        profiles={
            "research": PDFProfileConfig(
                ("docling", "pymupdf"),
                minimum_quality_score=0.75,
                preserve_tables=True,
                preserve_layout=True,
            )
        },
    )
    registry = HarborParserFactory().create_registry(
        ParserConfig(pdf=PDFParserConfig(router=router_config))
    )

    parser = registry.resolve("paper.pdf", "application/pdf")

    assert isinstance(parser, HarborPDFParser)
    assert parser.profile == "research"
    assert [engine.name for engine in parser.engines] == ["docling", "pymupdf"]


def test_ocr_first_profile_keeps_its_distinct_engine_policy() -> None:
    parser = HarborParserFactory().create_pdf_parser(profile=PDFParserProfile.OCR_FIRST)

    assert parser.profile is PDFParserProfile.OCR_FIRST
    assert [engine.name for engine in parser.engines] == [
        "paddleocr",
        "mineru",
        "docling",
        "pymupdf",
    ]


@pytest.mark.asyncio
async def test_parse_request_can_select_a_family_explicitly() -> None:
    registry = HarborParserFactory().create_registry()

    result = await registry.parse_request(
        ParseRequest(
            source_uri="memory://readme",
            parser="markup",
            engine="markdown",
            options={"content": "# Explicit family"},
        )
    )

    assert result.parser_name == "markup"
    assert result.engine_name == "markdown"


@pytest.mark.asyncio
async def test_pdf_parser_owns_quality_fallback_and_attempt_history() -> None:
    parser = HarborPDFParser(
        engines=(
            _ResultEngine("enough text but poor provider output", quality_score=0.2),
            _SecondResultEngine("enough text for the quality threshold"),
        ),
        min_content_chars=10,
    )

    result = await parser.parse(
        ParseRequest(
            source_uri="memory://report.pdf",
            filename="report.pdf",
            mime_type="application/pdf",
            options={"content": b"%PDF"},
        )
    )

    assert result.parser_name == "pdf"
    assert result.engine_name == "second"
    assert [attempt.engine for attempt in result.attempts] == ["result", "second"]
    assert result.attempts[0].success is True
    assert result.attempts[0].quality_score == pytest.approx(0.2)
    assert result.attempts[1].quality_score == pytest.approx(1.0)


def test_pdf_engine_registry_rejects_duplicate_provider_names() -> None:
    registry = PDFEngineRegistry((_ResultEngine("first"),))

    with pytest.raises(DuplicatePDFEngineError):
        registry.register(_ResultEngine("second"))
