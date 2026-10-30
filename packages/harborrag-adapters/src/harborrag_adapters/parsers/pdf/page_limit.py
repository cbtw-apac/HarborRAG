"""Refuse a PDF with too many pages before any engine spends time on it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO

from harborrag_adapters.parsers.common.limits import PDF_MAX_PAGES
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.errors import MaxPagesExceededError
from harborrag_core.domain.parser import ParseInput

type PageCounter = Callable[[bytes], int]


def count_with_pypdfium2(data: bytes) -> int:
    """Count pages with pypdfium2."""
    import pypdfium2

    document = pypdfium2.PdfDocument(data)
    try:
        return len(document)
    finally:
        document.close()


def count_with_pymupdf(data: bytes) -> int:
    """Count pages with PyMuPDF."""
    import fitz

    with fitz.open(stream=data, filetype="pdf") as document:
        return int(document.page_count)


def count_with_pypdf(data: bytes) -> int:
    """Count pages with pypdf."""
    from pypdf import PdfReader

    return len(PdfReader(BytesIO(data)).pages)


DEFAULT_PAGE_COUNTERS: tuple[PageCounter, ...] = (
    count_with_pypdfium2,
    count_with_pymupdf,
    count_with_pypdf,
)


@dataclass(frozen=True, slots=True)
class PdfPageLimit:
    """Reject a PDF whose page count is above ``max_pages``; ``None`` turns the check off.

    The first installed counter decides. A PDF none of them can read, such as
    an encrypted or damaged one, is left to the engines, which report why.
    """

    max_pages: int | None = PDF_MAX_PAGES
    counters: tuple[PageCounter, ...] = DEFAULT_PAGE_COUNTERS

    def __post_init__(self) -> None:
        if self.max_pages is not None and self.max_pages < 1:
            raise ValueError("PDF max_pages must be at least 1 or null")

    def check(self, parse_input: ParseInput) -> None:
        """Raise when ``parse_input`` has more pages than allowed.

        Raises:
            MaxPagesExceededError: The PDF has more than ``max_pages`` pages.
        """
        if self.max_pages is None:
            return
        page_count = self.count(read_parse_input_bytes(parse_input))
        if page_count is not None and page_count > self.max_pages:
            raise MaxPagesExceededError(
                page_count=page_count, max_pages=self.max_pages, engine="pdf"
            )

    def count(self, data: bytes) -> int | None:
        """Return the page count from the first installed counter, or None when none can read it."""
        for counter in self.counters:
            try:
                return counter(data)
            except ImportError:
                continue
            except Exception:  # noqa: BLE001 - an unreadable PDF is the engines' to report
                return None
        return None
