"""PyMuPDF provider configuration."""

from dataclasses import dataclass

# PyMuPDF runs in-process, so a page-count cap provides an early bound on
# degenerate inputs when no subprocess timeout is available.
DEFAULT_MAX_PAGES = 1000


@dataclass(slots=True)
class PyMuPDFConfig:
    """Configuration for the in-process PyMuPDF provider."""

    max_pages: int | None = DEFAULT_MAX_PAGES


__all__ = ["DEFAULT_MAX_PAGES", "PyMuPDFConfig"]
