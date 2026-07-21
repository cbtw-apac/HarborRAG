from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput

from ..exceptions import EncryptedPdfError, ParseError
from ..utils import compact_text
from .base import PdfBackend, PdfParseResult
from .utils import merge_dataclass_options, page_element

# PyMuPDF runs fully in-process (unlike the subprocess-based MinerU backend,
# which bounds itself with `timeout_seconds`), so there is no wall-clock
# timeout available here without a bigger refactor (e.g. running this backend
# in a worker process/thread with a hard deadline). A page-count cap is the
# main lever available in-process: it fails fast, before any per-page text
# extraction work, on a malicious/degenerate PDF that declares an enormous
# page count. Unlike Docling's `max_num_pages`/LiteParse's `max_pages` (which
# both default to `None`, i.e. unbounded), this defaults to a real bound
# because PyMuPDF is the only backend in the chain with no other safeguard
# (no subprocess timeout, no OCR-model-imposed cost ceiling).
DEFAULT_MAX_PAGES = 1000


@dataclass(slots=True)
class PyMuPdfBackendOptions:
    """Configuration for the in-process PyMuPDF backend."""

    max_pages: int | None = DEFAULT_MAX_PAGES


class PyMuPdfBackend(PdfBackend):
    """Fast local PDF text extractor backed by PyMuPDF."""

    name: ClassVar[str] = "pymupdf"

    def __init__(
        self,
        options: PyMuPdfBackendOptions | None = None,
        **overrides: Any,
    ) -> None:
        """Create a PyMuPDF backend from options plus keyword overrides."""

        self.options = merge_dataclass_options(
            options,
            PyMuPdfBackendOptions,
            overrides,
        )

    def parse(self, input: ParseInput) -> PdfParseResult:
        """Extract text directly from PDF pages without OCR."""

        pymupdf = self._import_pymupdf()
        try:
            document = pymupdf.open(stream=input.read_bytes(), filetype="pdf")
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
                raise ParseError(
                    f"PDF has {document.page_count} pages, exceeding the "
                    f"pymupdf backend's max_pages={self.options.max_pages} cap"
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

            warnings.extend(self._warnings(pymupdf))
            return PdfParseResult(
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
                    "`harborrag-adapters[parsers]` or `pip install PyMuPDF`."
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
