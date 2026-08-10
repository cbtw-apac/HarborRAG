from __future__ import annotations

from typing import Any, ClassVar

from harborrag_adapters.parsers.common.normalization import compact_text
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.validation import guard_input_size
from harborrag_adapters.parsers.errors import (
    EncryptedPdfError,
    MaxFileSizeExceededError,
    MaxPagesExceededError,
    NoExtractableTextError,
    ParseError,
)
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.engines.pymupdf.config import (
    PyMuPDFConfig,
)
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_adapters.parsers.pdf.normalization import page_element
from harborrag_adapters.parsers.pdf.utils import merge_dataclass_options
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput

PyMuPdfBackendOptions = PyMuPDFConfig


class PyMuPDFEngine(HarborPDFEngine):
    """Fast local PDF text extractor backed by PyMuPDF."""

    name: ClassVar[str] = "pymupdf"

    def __init__(
        self,
        options: PyMuPDFConfig | None = None,
        **overrides: Any,
    ) -> None:
        """Create a PyMuPDF backend from options plus keyword overrides."""

        self.options = merge_dataclass_options(
            options,
            PyMuPDFConfig,
            overrides,
        )

    def parse_input(self, input: ParseInput) -> PDFParseResult:
        """Extract text directly from PDF pages without OCR."""

        pymupdf = self._import_pymupdf()
        source_bytes = guard_input_size(read_parse_input_bytes(input))
        if (
            self.options.max_file_size is not None
            and len(source_bytes) > self.options.max_file_size
        ):
            # Checked before `pymupdf.open()` runs: a size rejection is a
            # configured policy, not a parse failure, and must not require
            # opening the file to discover.
            raise MaxFileSizeExceededError(
                size_bytes=len(source_bytes),
                max_bytes=self.options.max_file_size,
                engine=self.name,
            )
        try:
            document = pymupdf.open(stream=source_bytes, filetype="pdf")
        except Exception as exc:  # noqa: BLE001 - external parser boundary
            raise ParseError(f"PyMuPDF could not open PDF: {exc}") from exc

        try:
            if getattr(document, "needs_pass", False):
                # Encrypted: no engine in the chain can extract text, so fail
                # distinctly instead of letting page access crash mid-loop and
                # abort the whole fallback chain.
                raise EncryptedPdfError("PDF is password-protected")

            if self.options.max_pages is not None and document.page_count > self.options.max_pages:
                # Reject before the per-page extraction loop below: a
                # degenerate PDF with an enormous page count would otherwise
                # do unbounded work in-process with no timeout to interrupt it.
                raise MaxPagesExceededError(
                    page_count=document.page_count,
                    max_pages=self.options.max_pages,
                    engine=self.name,
                )

            sections: list[str] = []
            elements: list[DocumentElement] = []
            warnings: list[str] = []
            for page_index, page in enumerate(document, start=1):
                try:
                    page_text = compact_text(page.get_text("text") or "")
                except Exception as exc:  # noqa: BLE001 - per-page isolation
                    warnings.append(f"page {page_index} failed: {exc}")
                    continue
                if not page_text:
                    continue
                sections.append(f"Page {page_index}\n{page_text}")
                elements.append(page_element(self.name, page_index, page_text))

            if document.page_count > 0 and not sections:
                # A non-empty document with zero extracted text is a distinct
                # condition from an empty/corrupt file (which never reaches
                # here) -- most commonly a scanned/image-only PDF that this
                # non-OCR engine cannot read. Surfacing it as a typed error
                # instead of an empty success lets a fallback chain route to
                # an OCR-capable engine, and lets a caller distinguish it from
                # any other rejection.
                raise NoExtractableTextError(page_count=document.page_count)

            warnings.extend(self._warnings(pymupdf))
            return PDFParseResult(
                content="\n\n".join(sections).strip(),
                engine=self.name,
                elements=elements,
                metadata={
                    "page_count": document.page_count,
                    "pdf_metadata": dict(document.metadata or {}),
                },
                warnings=warnings,
            )
        finally:
            document.close()

    @staticmethod
    def _import_pymupdf() -> Any:
        """Import either modern `pymupdf` or legacy `fitz` package names."""

        try:
            import pymupdf

            return pymupdf
        except ImportError:
            try:
                import fitz

                return fitz
            except ImportError as exc:
                raise ImportError(
                    "PDF parsing with PyMuPDF requires `PyMuPDF`; install "
                    "`harborrag-adapters[pdf-pymupdf]` or `pip install PyMuPDF`."
                ) from exc

    @staticmethod
    def _warnings(pymupdf: Any) -> list[str]:
        """Collect MuPDF warnings when the binding exposes them."""

        tools = getattr(pymupdf, "TOOLS", None)
        warnings = getattr(tools, "mupdf_warnings", None)
        if not callable(warnings):
            return []
        message = warnings()
        return [message] if message else []


PyMuPdfBackend = PyMuPDFEngine
