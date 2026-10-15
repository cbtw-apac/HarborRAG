"""Unit tests for the PDF parser's request-based path, constructor guards, and routing."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from harborrag_adapters.parsers.common.models import ParseRequest
from harborrag_adapters.parsers.compat import PdfBackend, PdfParser, PdfParseResult
from harborrag_adapters.parsers.errors import (
    EncryptedPdfError,
    NoExtractableTextError,
    PDFParsingFailedError,
)
from harborrag_adapters.parsers.pdf.config import PDFProfileConfig, PDFRouterConfig
from harborrag_adapters.parsers.pdf.router import PDFEngineRegistry, PDFEngineRouter
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit

_USEFUL_TEXT = "Useful PDF content for downstream retrieval"


class _UsefulBackend(PdfBackend):
    name: ClassVar[str] = "useful_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        return PdfParseResult(
            content=_USEFUL_TEXT,
            engine=self.name,
            elements=[DocumentElement(id="pdf:useful:0", type="paragraph", content=_USEFUL_TEXT)],
            metadata={"page_count": 1},
            warnings=["useful_pdf: minor glyph substitution"],
        )


class _ShortBackend(PdfBackend):
    name: ClassVar[str] = "short_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        return PdfParseResult(content="tiny", engine=self.name)


class _MissingDependencyBackend(PdfBackend):
    name: ClassVar[str] = "missing_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        raise ImportError("install the pdf extra")


class _CrashingBackend(PdfBackend):
    name: ClassVar[str] = "crashing_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        raise RuntimeError("segfault in native layer")


class _EncryptedBackend(PdfBackend):
    name: ClassVar[str] = "encrypted_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        raise EncryptedPdfError("PDF is password protected")


class _NoTextBackend(PdfBackend):
    name: ClassVar[str] = "no_text_pdf"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        raise NoExtractableTextError(page_count=2)


class _MustNotRunBackend(PdfBackend):
    name: ClassVar[str] = "must_not_run"

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        raise AssertionError("engine should not run")


def _request(content: bytes, **options: object) -> ParseRequest:
    return ParseRequest(
        source_uri="memory://doc.pdf",
        filename="doc.pdf",
        mime_type="application/pdf",
        options={"content": content, **options},
    )


def test_constructor_rejects_both_engines_and_backends() -> None:
    with pytest.raises(ValueError, match="not both"):
        PdfParser(engines=[_UsefulBackend()], backends=[_UsefulBackend()])


def test_constructor_requires_configured_engines() -> None:
    with pytest.raises(ValueError, match="PDF engines are required"):
        PdfParser()


def test_explicit_router_is_used_instead_of_building_one() -> None:
    registry = PDFEngineRegistry((_ShortBackend(), _UsefulBackend()))
    router = PDFEngineRouter(
        registry,
        PDFRouterConfig(
            default_profile="liteparse",
            profiles={"liteparse": PDFProfileConfig(("useful_pdf",), minimum_quality_score=0.0)},
        ),
    )
    parser = PdfParser(engines=[_ShortBackend(), _UsefulBackend()], router=router)

    document = parser.parse_input(ParseInput(content=b"%PDF", filename="doc.pdf"))

    assert parser._router is router
    assert document.metadata["pdf_engine"] == "useful_pdf"
    # Only the router's explicit order ran, so the short engine left no trace.
    assert all("short_pdf" not in warning for warning in document.warnings or [])


def test_engines_and_backends_expose_the_registered_engines() -> None:
    parser = PdfParser(engines=[_ShortBackend(), _UsefulBackend()])

    assert [engine.name for engine in parser.engines] == ["short_pdf", "useful_pdf"]
    assert [engine.name for engine in parser.backends] == ["short_pdf", "useful_pdf"]


@pytest.mark.parametrize(
    ("filename", "mime_type", "expected"),
    [
        ("report.PDF", None, True),
        ("report.bin", "application/pdf; charset=binary", True),
        ("report.bin", "APPLICATION/X-PDF", True),
        ("report.bin", "", False),
        ("report.txt", "text/plain", False),
    ],
)
def test_supports_matches_suffix_or_normalized_mime_type(
    filename: str, mime_type: str | None, expected: bool
) -> None:
    parser = PdfParser(engines=[_UsefulBackend()])

    assert parser.supports(Path(filename), mime_type) is expected


def test_parse_input_reraises_encrypted_pdf_without_trying_later_engines() -> None:
    parser = PdfParser(engines=[_EncryptedBackend(), _MustNotRunBackend()])

    with pytest.raises(EncryptedPdfError, match="password protected"):
        parser.parse_input(ParseInput(content=b"%PDF", filename="locked.pdf"))


def test_parse_input_records_unavailable_engine_and_falls_back() -> None:
    parser = PdfParser(engines=[_MissingDependencyBackend(), _UsefulBackend()])

    document = parser.parse_input(ParseInput(content=b"%PDF", filename="doc.pdf"))

    assert document.content == _USEFUL_TEXT
    assert document.warnings is not None
    assert document.warnings[0] == "missing_pdf: unavailable (install the pdf extra)"


@pytest.mark.asyncio
async def test_parse_returns_empty_result_for_zero_byte_request() -> None:
    parser = PdfParser(engines=[_MustNotRunBackend()])

    result = await parser.parse(_request(b"", metadata={"tenant": "acme"}))

    assert result.text == ""
    assert result.engine_name == "empty-input"
    assert result.metadata["tenant"] == "acme"
    assert [attempt.engine for attempt in result.attempts] == ["empty-input"]
    assert result.warnings == []


@pytest.mark.asyncio
async def test_parse_ignores_non_mapping_metadata_on_empty_request() -> None:
    parser = PdfParser(engines=[_MustNotRunBackend()])
    request = ParseRequest(source_uri="memory://doc.pdf", options={"content": b""})
    request.options["metadata"] = ["not", "a", "mapping"]

    # A non-mapping metadata payload makes the request ambiguous, so it is
    # not short-circuited as empty; the engine chain runs and fails instead.
    with pytest.raises(PDFParsingFailedError):
        await parser.parse(request)


@pytest.mark.asyncio
async def test_parse_falls_back_through_every_failure_kind_to_useful_engine() -> None:
    parser = PdfParser(
        engines=[
            _NoTextBackend(),
            _MissingDependencyBackend(),
            _CrashingBackend(),
            _ShortBackend(),
            _UsefulBackend(),
        ],
    )

    result = await parser.parse(_request(b"%PDF-1.4", metadata={"source": "upload"}))

    assert result.text == _USEFUL_TEXT
    assert result.engine_name == "useful_pdf"
    assert result.metadata["source"] == "upload"
    assert result.metadata["pdf_engine"] == "useful_pdf"
    assert [(attempt.engine, attempt.success) for attempt in result.attempts] == [
        ("no_text_pdf", False),
        ("missing_pdf", False),
        ("crashing_pdf", False),
        ("short_pdf", True),
        ("useful_pdf", True),
    ]
    assert isinstance(result.attempts[0].error, NoExtractableTextError)
    assert result.warnings[1] == "missing_pdf: unavailable (install the pdf extra)"
    assert result.warnings[2] == "crashing_pdf: failed (segfault in native layer)"
    assert result.warnings[3].startswith("short_pdf: ")
    assert result.warnings[-1] == "useful_pdf: minor glyph substitution"


@pytest.mark.asyncio
async def test_parse_reraises_encrypted_pdf() -> None:
    parser = PdfParser(engines=[_EncryptedBackend(), _MustNotRunBackend()])

    with pytest.raises(EncryptedPdfError):
        await parser.parse(_request(b"%PDF-1.4"))


@pytest.mark.asyncio
async def test_parse_raises_aggregate_failure_when_no_engine_is_acceptable() -> None:
    parser = PdfParser(engines=[_CrashingBackend(), _ShortBackend()])

    with pytest.raises(PDFParsingFailedError) as excinfo:
        await parser.parse(_request(b"%PDF-1.4"))

    message = str(excinfo.value)
    assert "crashing_pdf (failed (segfault in native layer))" in message
    assert "short_pdf" in message


@pytest.mark.asyncio
async def test_parse_skips_size_guard_for_unconvertible_remote_request() -> None:
    parser = PdfParser(engines=[_UsefulBackend()])
    # A remote URI without caller content can't become a ParseInput, so the
    # size guard steps aside and the engine reports the failure itself.
    request = ParseRequest(source_uri="https://example.test/doc.pdf", filename="doc.pdf")

    with pytest.raises(PDFParsingFailedError, match="useful_pdf"):
        await parser.parse(request)
