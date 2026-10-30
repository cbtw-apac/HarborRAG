"""PyMuPDF provider configuration."""

from dataclasses import dataclass

from harborrag_adapters.parsers.common.limits import PDF_MAX_PAGES

# PyMuPDF runs in-process, so a page-count cap provides an early bound on
# degenerate inputs when no subprocess timeout is available.
DEFAULT_MAX_PAGES = PDF_MAX_PAGES


@dataclass(slots=True)
class PyMuPDFConfig:
    """Configuration for the in-process PyMuPDF provider."""

    max_pages: int | None = DEFAULT_MAX_PAGES
    # Unset by default: pymupdf has never enforced a size cap of its own
    # (only the parser-family-wide byte guard applies), so this stays opt-in
    # to avoid changing behavior for anyone not explicitly configuring it.
    max_file_size: int | None = None


PyMuPdfBackendOptions = PyMuPDFConfig

__all__ = ["DEFAULT_MAX_PAGES", "PyMuPDFConfig", "PyMuPdfBackendOptions"]
