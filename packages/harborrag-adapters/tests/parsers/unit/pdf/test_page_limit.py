"""A PDF above the page limit is refused before any engine reads it."""

from __future__ import annotations

from io import BytesIO
from typing import ClassVar

import pytest
from pypdf import PdfWriter

from harborrag_adapters.parsers.common.models import ParseRequest
from harborrag_adapters.parsers.compat import PdfBackend, PdfParser, PdfParseResult
from harborrag_adapters.parsers.errors import MaxPagesExceededError
from harborrag_adapters.parsers.pdf.page_limit import PdfPageLimit
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class _RecordingBackend(PdfBackend):
    name: ClassVar[str] = "recording_pdf"
    calls: ClassVar[int] = 0

    def parse_input(self, input: ParseInput) -> PdfParseResult:
        type(self).calls += 1
        return PdfParseResult(content="Text that is long enough to keep", engine=self.name)


def test_the_default_limit_is_200_pages() -> None:
    limit = PdfPageLimit()

    limit.check(ParseInput(content=_pdf(200), filename="ok.pdf"))
    with pytest.raises(MaxPagesExceededError) as raised:
        limit.check(ParseInput(content=_pdf(201), filename="long.pdf"))

    assert (raised.value.page_count, raised.value.max_pages) == (201, 200)


def test_an_unreadable_pdf_is_left_to_the_engines() -> None:
    PdfPageLimit(max_pages=1).check(ParseInput(content=b"%PDF-1.7 broken", filename="x.pdf"))


def test_the_first_installed_counter_decides() -> None:
    def missing(data: bytes) -> int:
        raise ImportError("not installed")

    limit = PdfPageLimit(max_pages=5, counters=(missing, lambda data: 7))

    with pytest.raises(MaxPagesExceededError):
        limit.check(ParseInput(content=b"%PDF", filename="x.pdf"))


def test_the_parser_refuses_a_long_pdf_before_any_engine_runs() -> None:
    _RecordingBackend.calls = 0
    parser = PdfParser(backends=[_RecordingBackend()], min_content_chars=10)

    with pytest.raises(MaxPagesExceededError):
        parser.parse_input(ParseInput(content=_pdf(201), filename="long.pdf"))
    unlimited = PdfParser(backends=[_RecordingBackend()], min_content_chars=10, max_pages=None)
    unlimited.parse_input(ParseInput(content=_pdf(201), filename="long.pdf"))

    assert _RecordingBackend.calls == 1


@pytest.mark.asyncio
async def test_the_async_path_refuses_a_long_pdf_too() -> None:
    parser = PdfParser(backends=[_RecordingBackend()], min_content_chars=10)
    request = ParseRequest(
        source_uri="memory://long.pdf", filename="long.pdf", options={"content": _pdf(201)}
    )

    with pytest.raises(MaxPagesExceededError):
        await parser.parse(request)
