"""Defaults every connector shares: the size of one document and the file types it reads."""

from __future__ import annotations

DEFAULT_MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".markdown",
    ".txt",
    ".pdf",
    ".docx",
    ".doc",
    ".epub",
    ".odt",
    ".pptx",
    ".csv",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".html",
)
